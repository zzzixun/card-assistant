"""行動支付優惠爬蟲：卡優新聞網「優支付」列表（https://www.cardu.com.tw/mpay/list.php）。

為什麼不直接爬 LINE Pay / 全支付 / 悠遊付官網：活動大多只在 App 內，官網是 JS 動態頁。
卡優把各家活動整理成伺服器端渲染的文章，並附「情報來源」連結回官方公告。

禮貌原則：自訂 User-Agent、每次請求間隔 1.5 秒、已抓過的文章快取不重抓、只抓最新幾頁。
只存結構化欄位 + 短摘要 + 原文連結，不轉存全文。

執行：python -m card_assistant.promos --pages 3
"""
from __future__ import annotations

import argparse
import html
import json
import re
import time
from datetime import date
from pathlib import Path

BASE = "https://www.cardu.com.tw"
UA = "card-assistant/0.1 (personal side project; https://github.com/zzzixun/card-assistant)"
OUT = Path(__file__).resolve().parent.parent / "data" / "promos.json"
# 優支付的分類列表（總列表混了信用卡、集點新聞，分類列表才幾乎都是行動支付）
CATEGORIES = {"購物": "nt2019092314493589", "美食": "nt2019092314494566",
              "生活": "nt2019092314502593", "交通": "nt2019092314520715"}

PAYS = {  # 關鍵字 → engine 的支付 id
    "linepay": ["LINE Pay", "LINEPay"],
    "pxpay_plus": ["全支付", "PX Pay Plus"],
    "easywallet": ["悠遊付"],
    "easycard": ["悠遊卡", "嗶悠遊"],
    "jkopay": ["街口"],
    "icashpay": ["icash Pay", "icashPay"],
    "taishinpay": ["台新Pay"],
    "twpay": ["台灣Pay", "TWQR"],
    "ipass": ["iPASS MONEY", "一卡通"],
    "pipay": ["Pi 拍錢包", "Pi錢包"],
    "applepay": ["Apple Pay"],
}
BANKS = {  # 關鍵字 → 卡片 id（對應 cards.yaml）
    "台新": ["richart", "friday"], "玉山": ["unicard", "ubear"], "國泰": ["cube"],
    "聯邦": ["linebank"], "富邦": ["momo"], "中國信託": ["uniopen"], "中信": ["uniopen"],
}
ANY_CARD = ["任一發卡銀行", "任一銀行", "不限銀行", "不限卡別", "任一信用卡"]
REGISTER = ["登錄", "報名", "領券", "領取優惠券", "序號", "輸入優惠碼"]


# ---------- 解析（純函式，可測試） ----------

def _text(fragment: str) -> str:
    fragment = re.sub(r"<script.*?</script>|<style.*?</style>|<!--.*?-->", "", fragment, flags=re.S)
    t = html.unescape(re.sub(r"<[^>]+>", "\n", fragment))
    return re.sub(r"\n\s*\n+", "\n", t).strip()


def parse_period(s: str) -> tuple[str | None, str | None]:
    """'即日起~2026-11-13' / '2026-11-01~2026-11-30' → (start, end) ISO 字串。"""
    ds = re.findall(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    iso = [f"{y}-{int(m):02d}-{int(d):02d}" for y, m, d in ds]
    if not iso:
        return None, None
    if len(iso) == 1:
        return (None, iso[0]) if "~" in s or "起" in s else (iso[0], iso[0])
    return iso[0], iso[-1]


def parse_list(page_html: str, category: str = "") -> list[dict]:
    """列表頁 → [{id, title, end, url}]，留優支付（/mpay/）與信用卡（/message/）文章。"""
    pat = (r'<h3><a[^>]+href="/(mpay|message)/detail\.php\?(\d+)"[^>]*>(.*?)</a>.*?'
           r'<span class="mb-1">(.*?)</span>')
    out = []
    for sec, pid, title, period in re.findall(pat, page_html, re.S):
        start, end = parse_period(period)
        out.append({"id": pid, "title": _text(title), "start": start, "end": end,
                    "category": category, "url": f"{BASE}/{sec}/detail.php?{pid}"})
    return list({x["id"]: x for x in out}.values())


def parse_detail(page_html: str) -> dict:
    """文章頁 → 期間、發布日、內文、官方來源連結。"""
    txt = _text(page_html)
    period = re.search(r"活動期間：(.+)", txt)
    published = re.search(r"發布日期：(\d{4}-\d{2}-\d{2})", txt)
    body = txt
    if published:
        body = txt[published.end():]
    body = re.split(r"◎延伸閱讀|◎情報來源|☆謹慎理財", body)[0].strip()
    src = re.search(r'情報來源：.*?href="([^"]+)"', page_html, re.S)
    start, end = parse_period(period.group(1)) if period else (None, None)
    return {"start": start, "end": end, "published": published.group(1) if published else None,
            "body": body, "source": src.group(1) if src else None}


def _num(s: str) -> int:
    return int(s.replace(",", ""))


def extract(title: str, body: str) -> dict:
    """從標題+內文抽出：支付、銀行/卡片、最高回饋率、門檻、上限、是否需登錄。"""
    t = f"{title}\n{body}"
    # 支付與銀行「標題優先」：標題有提到就只用標題；否則才看內文，
    # 且內文列出超過 2 家銀行時（多半是「適用銀行一覽」）視為不限定。
    pays = _find_pays(title) or _find_pays(body)
    banks = _find_banks(title)
    if not banks:
        banks = _find_banks(body)
        banks = banks if len(banks) <= 2 else set()
    rates = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s*%", t) if 0 < float(x) <= 100]
    mins = re.findall(r"滿\s*(?:新台幣|NT\$)?\s*([\d,]+)\s*元", t)
    caps = re.findall(r"上限\s*(?:新台幣|NT\$)?\s*([\d,]+)\s*(?:元|點)", t)
    return {
        "pays": pays,
        "cards": sorted({c for b in banks for c in BANKS[b]}),
        "any_card": any(k in t for k in ANY_CARD),
        "max_rate": max(rates) / 100 if rates else None,
        "min_amount": _num(mins[0]) if mins else None,
        "cap": _num(caps[0]) if caps else None,
        "register": any(k in t for k in REGISTER),
        "limited": bool(re.search(r"限量|名額|額滿", t)),
    }


def _find_pays(t: str) -> list[str]:
    low = t.lower()
    return [pid for pid, kws in PAYS.items() if any(k.lower() in low for k in kws)]


def _find_banks(t: str) -> set[str]:
    banks = {k for k in BANKS if k in t}
    return banks - {"中信"} if "中國信託" in t else banks


def match_merchants(text: str, names: list[str]) -> list[str]:
    """內文提到哪些已知店名（只比對長度 ≥ 2 的名稱，避免誤判）。"""
    low = text.lower()
    return sorted({n for n in names if len(n) >= 2 and n.lower() in low})


# ---------- 爬取 ----------

def _get(url: str) -> str:
    import requests
    r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
    r.raise_for_status()
    r.encoding = "utf-8"
    return r.text


def crawl(pages: int = 5, delay: float = 1.5, out: Path = OUT, today: date | None = None,
          merchant_names: list[str] | None = None) -> list[dict]:
    today = today or date.today()
    cache = {p["id"]: p for p in load(out)}
    listed: list[dict] = []
    for cat, pk in CATEGORIES.items():
        for n in range(1, pages + 1):
            items = parse_list(_get(f"{BASE}/mpay/list.php?mt_pk={pk}&PageNo={n}"), cat)
            listed += items
            time.sleep(delay)
            # 沒東西或整頁都過期 → 這個分類不用再往後翻
            if not items or all(i["end"] and i["end"] < today.isoformat() for i in items):
                break

    for item in listed:
        if item["id"] in cache or (item["end"] and item["end"] < today.isoformat()):
            continue
        d = parse_detail(_get(item["url"]))
        time.sleep(delay)
        promo = {**item, "start": d["start"] or item["start"], "end": d["end"] or item["end"],
                 "published": d["published"], "source": d["source"],
                 "summary": d["body"][:160], **extract(item["title"], d["body"]),
                 "merchants": match_merchants(f"{item['title']}\n{d['body']}", merchant_names or [])}
        cache[item["id"]] = promo
        print(f"  + {promo['title'][:40]}  ~{promo['end']}")

    # 只保留未過期的
    keep = [p for p in cache.values() if not p["end"] or p["end"] >= today.isoformat()]
    keep.sort(key=lambda p: p["end"] or "9999")
    out.write_text(json.dumps(keep, ensure_ascii=False, indent=1), encoding="utf-8")
    return keep


def load(path: Path = OUT) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8")) if Path(path).exists() else []


def relevant(promos: list[dict], state: dict, merchant: str | None = None,
             on: date | None = None) -> list[dict]:
    """篩出跟我有關的：用我有的支付、或我有的卡、或不限卡；可再限定店名。"""
    on_s = (on or date.today()).isoformat()
    my_pays, my_cards = set(state.get("pays", [])), set(state.get("cards", []))
    out = []
    for p in promos:
        if p.get("end") and p["end"] < on_s or p.get("start") and p["start"] > on_s:
            continue
        if merchant and merchant not in p.get("merchants", []) and merchant.lower() not in p["title"].lower():
            continue
        mine_pay = set(p["pays"]) & my_pays
        mine_card = set(p["cards"]) & my_cards
        if mine_pay or mine_card or (p["any_card"] and not p["cards"]):
            out.append({**p, "mine_pay": sorted(mine_pay), "mine_card": sorted(mine_card)})
    return out


if __name__ == "__main__":
    import sys

    from card_assistant.engine import load_db, merchant_index
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="爬卡優「優支付」行動支付優惠")
    ap.add_argument("--pages", type=int, default=3, help="每個分類最多翻幾頁（每頁 12 篇）")
    args = ap.parse_args()
    names = sorted({v[0] for v in merchant_index(load_db(OUT.parent)).values()})
    res = crawl(args.pages, merchant_names=names)
    print(f"完成：{len(res)} 筆未過期優惠 → {OUT}")
