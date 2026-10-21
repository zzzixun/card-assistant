"""刷卡回饋規則引擎。

資料分兩層：
- 規則庫（data/cards.yaml、merchants.yaml、campaigns.yaml）：公開、可放 Git。
- 個人狀態（data/my_state.yaml）：持有哪些卡/支付、已登錄活動、本月已用額度。不進 Git。

核心函式 recommend()：給定一筆消費（店名、金額、日期），列出所有
「卡 × 權益方案 × 支付方式」組合的預估回饋，由高到低排序。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

CARD_PAY = "card"  # 直接刷實體卡（含 Apple Pay / Google Pay）


# ---------- 載入 ----------

def load_yaml(path: Path | str):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_db(data_dir: Path | str) -> dict:
    d = Path(data_dir)
    db = {
        "cards": load_yaml(d / "cards.yaml")["cards"],
        "merchants": load_yaml(d / "merchants.yaml"),
        "campaigns": (load_yaml(d / "campaigns.yaml") or {}).get("campaigns", []),
        "holidays": [],
    }
    db["holidays"] = [_d(x) for x in load_yaml(d / "cards.yaml").get("holidays", [])]
    return db


def load_state(data_dir: Path | str) -> dict:
    d = Path(data_dir)
    p = d / "my_state.yaml"
    return load_yaml(p if p.exists() else d / "my_state.example.yaml")


def _d(x) -> date:
    return x if isinstance(x, date) else date.fromisoformat(str(x))


# ---------- 店名解析 ----------

def _norm(s: str) -> str:
    return str(s).lower().replace(" ", "").replace("-", "")


def merchant_index(db: dict) -> dict[str, tuple[str, set]]:
    """norm(名稱或別名) -> (標準名, 類別集合)。規則裡出現的店名也自動收錄。"""
    idx: dict[str, tuple[str, set]] = {}
    for c in db["cards"]:
        for r in _all_rules(c):
            for m in r.get("merchants", []):
                idx.setdefault(_norm(m), (m, set()))
    for name, info in (db["merchants"] or {}).items():
        info = info or {}
        entry = (name, set(info.get("cats", [])))
        for alias in [name, *info.get("aliases", [])]:
            idx[_norm(alias)] = entry
    return idx


def resolve_merchant(db: dict, query: str) -> tuple[str, set]:
    """回傳 (標準店名, 類別)。先比對全名/別名，再做子字串比對，找不到就原樣回傳。"""
    idx = merchant_index(db)
    q = _norm(query)
    if q in idx:
        return idx[q]
    hits = [v for k, v in idx.items() if q and (q in k or k in q)]
    if hits:
        return max(hits, key=lambda v: len(v[0]))  # 取最長、最具體的匹配
    return query, set()


def suggest_merchants(db: dict, query: str, limit: int = 10) -> list[str]:
    q = _norm(query)
    names = {v[0] for k, v in merchant_index(db).items() if q and q in k}
    return sorted(names)[:limit]


def expand_cats(cats: set) -> set:
    cats = set(cats)
    if cats & {"日本", "韓國", "歐洲"}:
        cats.add("海外")
    return cats


# ---------- 規則比對 ----------

def _all_rules(card: dict):
    yield from card.get("rules", [])
    for p in card.get("plans", []):
        yield from p.get("rules", [])


def _active(obj: dict, on: date, parent: dict | None = None) -> bool:
    start = obj.get("start") or (parent or {}).get("start")
    end = obj.get("end") or (parent or {}).get("end")
    return (not start or _d(start) <= on) and (not end or on <= _d(end))


def rule_matches(rule: dict, merchant: str, cats: set, pay: str, amount: float,
                 on: date, holidays: list) -> bool:
    if pay not in rule.get("pay", [CARD_PAY]):
        return False
    if amount < rule.get("min_amount", 0):
        return False
    if merchant in rule.get("exclude", []) or cats & set(rule.get("exclude_cats", [])):
        return False
    if "weekdays" in rule and on.weekday() not in rule["weekdays"]:
        return False
    if rule.get("holiday_only") and not (on.weekday() >= 5 or on in holidays):
        return False
    if rule.get("any_merchant"):
        return True
    return merchant in rule.get("merchants", []) or bool(cats & set(rule.get("cats", [])))


@dataclass
class Option:
    card: str
    card_name: str
    plan: str | None          # 需切換到的權益方案；None = 不用切換
    pay: str
    rate: float               # 實際回饋率（已考慮上限）
    reward: float             # 預估回饋（元）
    rule: str
    notes: list[str] = field(default_factory=list)
    cap_used: dict[str, float] = field(default_factory=dict)  # 這筆會吃掉的上限額度


def cap_group(rule: dict, card_id: str) -> str | None:
    if "cap" not in rule:
        return None
    return rule.get("cap_group", f"{card_id}:{rule['name']}")


def _cap_left(rule: dict, card_id: str, usage: dict) -> float:
    group = cap_group(rule, card_id)
    if group is None:
        return float("inf")
    return max(rule["cap"] - usage.get(group, 0), 0)


def _bonus(rule: dict, base: float, amount: float, card_id: str, usage: dict):
    """回傳 (加碼金額, 備註)。rate 為總回饋率；stack=True 的規則 rate 是額外疊加。"""
    extra = rule["rate"] if rule.get("stack") else rule["rate"] - base
    raw = amount * extra
    left = _cap_left(rule, card_id, usage)
    notes = []
    if raw > left:
        notes.append(f"「{rule['name']}」加碼上限剩 {left:.0f}，超出部分只剩基本回饋")
    return min(raw, left), notes


def evaluate(card: dict, plan: dict | None, merchant: str, cats: set, pay: str,
             amount: float, on: date, db: dict, state: dict) -> Option | None:
    usage = state.get("usage", {})
    holidays = db["holidays"]
    if merchant in card.get("exclude", []) or cats & set(card.get("exclude_cats", [])):
        return None
    base = card.get("base", 0) if pay in card.get("base_pay", [CARD_PAY, *_all_pays()]) else 0

    candidates = [r for r in card.get("rules", []) if _active(r, on, card)]
    if plan:
        candidates += [r for r in plan.get("rules", []) if _active(r, on, plan)]
    # 已登錄的活動當成額外規則
    registered = set(state.get("registered", []))
    for cp in db["campaigns"]:
        if cp["card"] == card["id"] and _active(cp, on) and (
                not cp.get("register") or cp["id"] in registered):
            # 綁方案的活動只加在該方案；不綁方案的只加在「不切換」那一列，避免重複
            if (plan and cp.get("plan") == plan["id"]) or (not plan and not cp.get("plan")):
                candidates.append({**cp["rule"], "name": cp["title"], "stack": True,
                                   "campaign": True})

    matched = [r for r in candidates
               if rule_matches(r, merchant, cats, pay, amount, on, holidays)]
    main = [r for r in matched if not r.get("stack")]
    stacks = [r for r in matched if r.get("stack")]
    if plan and not main and not stacks:
        return None  # 這個方案對這家店沒加碼，不必列出

    notes: list[str] = []
    best_main, best_bonus = None, 0.0
    for r in main:
        b, n = _bonus(r, base, amount, card["id"], usage)
        if b > best_bonus or best_main is None:
            best_main, best_bonus, best_notes = r, b, n
    if best_main:
        notes += best_notes
        notes += [best_main["note"]] if best_main.get("note") else []
    total_bonus = best_bonus
    names = [best_main["name"]] if best_main else []
    used: dict[str, float] = {}
    if best_main and cap_group(best_main, card["id"]):
        used[cap_group(best_main, card["id"])] = best_bonus
    for r in stacks:
        b, n = _bonus(r, base, amount, card["id"], usage)
        total_bonus += b
        if cap_group(r, card["id"]):
            used[cap_group(r, card["id"])] = used.get(cap_group(r, card["id"]), 0) + b
        names.append(r["name"])
        notes += n + ([r["note"]] if r.get("note") else [])

    reward = amount * base + total_bonus
    if plan and plan.get("note"):
        notes.append(plan["note"])
    if card.get("verify"):
        notes.append("⚠ 規則待官網確認")
    return Option(card["id"], card["name"], plan["name"] if plan else None, pay,
                  reward / amount if amount else 0, round(reward, 1),
                  " + ".join(names) or "一般消費", notes, used)


def _all_pays():
    return ["linepay", "pxpay_plus", "easywallet", "taishinpay", "icashpay", "jkopay"]


def recommend(db: dict, state: dict, merchant_query: str, amount: float,
              on: date | None = None, extra_cats: set | None = None,
              pays: list[str] | None = None) -> list[Option]:
    """列出所有持有卡片的可行刷法，依預估回饋排序。

    pays：這家店可用的支付方式（預設 = 我持有的全部）。
    """
    on = on or date.today()
    if "usage_month" in state:
        state = {**state, "usage": current_usage(state, on)}
    merchant, cats = resolve_merchant(db, merchant_query)
    cats = expand_cats(cats | (extra_cats or set()))
    owned_cards = set(state.get("cards", [c["id"] for c in db["cards"]]))
    owned_pays = [CARD_PAY, *state.get("pays", [])]
    pays = [p for p in (pays or owned_pays) if p in owned_pays]
    accepts = ((db["merchants"] or {}).get(merchant) or {}).get("accepts")
    if accepts:  # 店家只收某些支付方式
        pays = [p for p in pays if p in accepts]

    out: list[Option] = []
    for card in db["cards"]:
        if card["id"] not in owned_cards:
            continue
        plans = [p for p in card.get("plans", []) if _active(p, on, card)]
        fixed = state.get("fixed_plans", {}).get(card["id"])  # 月結型（如 Unicard）由使用者固定
        if fixed:
            plans = [p for p in plans if p["id"] == fixed]
        plans = plans or [None]
        for pay in pays:
            if pay != CARD_PAY and pay not in card.get("bindable", _all_pays()):
                continue
            opts = [evaluate(card, p, merchant, cats, pay, amount, on, db, state)
                    for p in plans]
            if card.get("plans") and not fixed:  # 切換型卡片：再算一個「不切換」的基本回饋
                opts.append(evaluate(card, None, merchant, cats, pay, amount, on, db, state))
            out += [o for o in opts if o]

    # 未登錄但符合的活動 → 提醒
    registered = set(state.get("registered", []))
    for cp in db["campaigns"]:
        if (cp.get("register") and cp["id"] not in registered and cp["card"] in owned_cards
                and _active(cp, on)
                and any(rule_matches(cp["rule"], merchant, cats, p, amount, on, db["holidays"])
                        for p in pays)):
            for o in out:
                if o.card == cp["card"]:
                    o.notes.append(f"📌 未登錄活動：{cp['title']}（+{cp['rule']['rate']:.1%}）")

    # 同卡同方案同規則、回饋一樣的不同支付方式合併成一列（例：LINE Pay / 全支付 / 悠遊付）
    out.sort(key=lambda o: (-o.reward, o.plan is not None))
    merged: dict[tuple, Option] = {}
    for o in out:
        key = (o.card, o.plan, o.rule, o.reward)
        if key in merged:
            merged[key].pay += f" / {o.pay}"
        else:
            merged[key] = o
    return list(merged.values())


def expiring(db: dict, on: date | None = None, within_days: int = 45) -> list[dict]:
    """列出即將到期（或已過期）的方案/規則/活動，提醒去官網看新權益。"""
    on = on or date.today()
    rows = []

    def add(kind, card, name, obj, parent=None):
        end = obj.get("end") or (parent or {}).get("end")
        if end and (_d(end) - on).days <= within_days:
            rows.append({"類型": kind, "卡片": card, "項目": name, "到期日": _d(end),
                         "剩餘天數": (_d(end) - on).days,
                         "來源": obj.get("source") or (parent or {}).get("source", "")})

    for c in db["cards"]:
        add("卡片權益", c["name"], "整體權益", c)
        for p in c.get("plans", []):
            add("權益方案", c["name"], p["name"], p, c)
        for r in c.get("rules", []):
            if r.get("end"):
                add("規則", c["name"], r["name"], r, c)
    for cp in db["campaigns"]:
        add("活動", cp["card"], cp["title"], cp)
    return sorted(rows, key=lambda r: r["到期日"])


# ---------- 上限用量（每月自動歸零） ----------

def current_usage(state: dict, on: date | None = None) -> dict:
    """state["usage_month"] 不是本月 → 視為已歸零。"""
    month = (on or date.today()).strftime("%Y-%m")
    return dict(state.get("usage", {})) if state.get("usage_month") == month else {}


def record(state: dict, option: Option, on: date | None = None) -> dict:
    """「我刷了這筆」：把這筆吃掉的加碼額度累加進 state（回傳新 state，不改原物件）。"""
    usage = current_usage(state, on)
    for g, v in option.cap_used.items():
        usage[g] = round(usage.get(g, 0) + v, 1)
    return {**state, "usage": usage, "usage_month": (on or date.today()).strftime("%Y-%m")}


def save_state(data_dir: Path | str, state: dict) -> None:
    with open(Path(data_dir) / "my_state.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(state, f, allow_unicode=True, sort_keys=False)
