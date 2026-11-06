"""刷卡建議助理 - Streamlit 介面。執行：python -m streamlit run app.py"""
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

from card_assistant.engine import (current_usage, expiring, load_db, load_state, recommend,
                                   record, resolve_merchant, save_state, suggest_merchants)

from card_assistant.promos import OUT as PROMOS_PATH
from card_assistant.promos import load as load_promos
from card_assistant.promos import relevant

DATA = Path(__file__).parent / "data"
PAY_LABEL = {"card": "刷卡/Apple Pay", "linepay": "LINE Pay", "pxpay_plus": "全支付",
             "easywallet": "悠遊付", "taishinpay": "台新Pay", "icashpay": "icash Pay",
             "jkopay": "街口"}
CATS = ["餐飲", "外送", "超商", "超市", "百貨", "網購", "影音", "海外", "日本", "韓國",
        "加油", "交通", "藥妝", "旅遊"]


def pay_text(p: str) -> str:
    return " / ".join(PAY_LABEL.get(x, x) for x in p.split(" / "))


st.set_page_config(page_title="刷卡建議助理", page_icon="💳", layout="wide")
db = load_db(DATA)
# 有 my_state.yaml（本機）→ 讀寫檔案；沒有（公開 demo）→ 用範例檔，記錄只存在這次瀏覽
PERSIST = (DATA / "my_state.yaml").exists()
if "state" not in st.session_state:
    st.session_state.state = load_state(DATA)
state = st.session_state.state

with st.sidebar:
    st.subheader("📊 本月已用加碼額度")
    usage = current_usage(state)
    if usage:
        st.dataframe(pd.DataFrame(usage.items(), columns=["上限群組", "已用（點/元）"]),
                     hide_index=True, width="stretch")
    else:
        st.caption("本月還沒有紀錄。在查詢結果按「我刷了這筆」就會累計。")
    st.caption("每月 1 日自動歸零（以日曆月估算，帳單週期不同的卡會有誤差）。")
    if not PERSIST:
        st.info("Demo 模式：紀錄不會存檔。")

st.title("💳 刷卡建議助理")

tab_q, tab_promo, tab_plan, tab_todo = st.tabs(
    ["🔍 刷哪張卡", "🎁 行動支付優惠", "🔄 權益方案總覽", "📌 待登錄 / 即將到期"])
promos = load_promos()
CARD_NAME = {c["id"]: c["name"] for c in db["cards"]}


def promo_table(rows):
    return pd.DataFrame([{
        "截止": p.get("end") or "—", "活動": p["title"],
        "跟我有關": "、".join([PAY_LABEL.get(x, x) for x in p.get("mine_pay", [])]
                         + [CARD_NAME.get(x, x) for x in p.get("mine_card", [])]) or "不限卡",
        "最高": f"{p['max_rate']:.0%}" if p.get("max_rate") else "",
        "門檻": p.get("min_amount"), "上限": p.get("cap"),
        "需登錄": "✔" if p.get("register") else "", "限量": "✔" if p.get("limited") else "",
        "連結": p["url"],
    } for p in rows])

# ───────── 1. 查詢 ─────────
with tab_q:
    c1, c2, c3 = st.columns([3, 1, 1])
    q = c1.text_input("店名", placeholder="例如：海底撈、全聯、蝦皮、小七、東京")
    amount = c2.number_input("金額", min_value=1, value=1000, step=100)
    on = c3.date_input("消費日", value=date.today())
    with st.expander("進階：類別 / 支付方式"):
        extra = set(st.multiselect("店家類別（找不到店名時手動指定）", CATS))
        owned = ["card", *state.get("pays", [])]
        pays = st.multiselect("這家店可以用的支付方式", owned, default=owned, format_func=PAY_LABEL.get)

    if q:
        name, cats = resolve_merchant(db, q)
        hint = f"辨識為 **{name}**" + (f"（{'、'.join(sorted(cats))}）" if cats else "（未知類別，可在進階手動指定）")
        sug = [s for s in suggest_merchants(db, q) if s != name]
        st.caption(hint + (f"　也許是：{'、'.join(sug)}" if sug else ""))

        opts = recommend(db, state, q, amount, on, extra, pays)
        if opts:
            best = opts[0]
            st.success(f"### 👉 {best.card_name}　{('切到「' + best.plan + '」　') if best.plan else ''}"
                       f"用 {pay_text(best.pay)}\n回饋 **{best.rate:.1%}** ≈ **{best.reward:.0f} 元**（{best.rule}）")
            df = pd.DataFrame([{
                "卡片": o.card_name, "要切的方案": o.plan or "—", "支付": pay_text(o.pay),
                "回饋率": f"{o.rate:.2%}", "預估回饋": o.reward, "命中規則": o.rule,
                "備註": "；".join(dict.fromkeys(o.notes)),
            } for o in opts])
            st.dataframe(df, hide_index=True, width="stretch")

            labels = [f"{o.card_name}｜{o.plan or '不切換'}｜{pay_text(o.pay)}｜{o.reward:.0f} 元"
                      for o in opts]
            b1, b2 = st.columns([4, 1])
            pick = b1.selectbox("實際刷了哪一個？", range(len(opts)), format_func=labels.__getitem__)
            if b2.button("✅ 我刷了這筆", width="stretch"):
                st.session_state.state = record(state, opts[pick], on)
                if PERSIST:
                    save_state(DATA, st.session_state.state)
                used = opts[pick].cap_used
                st.toast("已記錄，扣掉上限：" + ("、".join(f"{k} {v:.0f}" for k, v in used.items())
                                              if used else "這筆沒有上限限制"))
                st.rerun()

        related = relevant(promos, state, name, on)
        if related:
            st.markdown(f"#### 🎁 {name} 相關的行動支付 / 刷卡活動（{len(related)}）")
            st.dataframe(promo_table(related), hide_index=True, width="stretch",
                         column_config={"連結": st.column_config.LinkColumn(display_text="看活動")})
            st.caption("活動條件由文章自動擷取，可能不完整，請點連結確認。這些活動不會算進上面的排名。")

# ───────── 行動支付優惠 ─────────
with tab_promo:
    if not promos:
        st.info("還沒有優惠資料。在終端機執行：python -m card_assistant.promos")
    else:
        f1, f2, f3 = st.columns([2, 2, 1])
        kw = f1.text_input("搜尋（店名、活動）", key="promo_kw")
        only_mine = f2.toggle("只看跟我的卡 / 支付有關的", value=True)
        need_reg = f3.toggle("只看需登錄")
        rows = relevant(promos, state) if only_mine else promos
        if kw:
            rows = [p for p in rows if kw.lower() in (p["title"] + p.get("summary", "")).lower()
                    or kw in p.get("merchants", [])]
        if need_reg:
            rows = [p for p in rows if p.get("register")]
        st.caption(f"共 {len(rows)} 筆（資料來源：卡優新聞網「優支付」，"
                   f"最後更新 {date.fromtimestamp(PROMOS_PATH.stat().st_mtime)}）")
        st.dataframe(promo_table(rows), hide_index=True, width="stretch",
                     column_config={"連結": st.column_config.LinkColumn(display_text="看活動")})

# ───────── 2. 方案總覽 ─────────
with tab_plan:
    switchable = [c for c in db["cards"] if c.get("plans")]
    card = st.selectbox("切換型卡片", switchable, format_func=lambda c: c["name"])
    st.info(f"切換方式：{card.get('switch', '—')}")
    kw = st.text_input("在方案裡搜尋店名", key="plan_kw")
    for p in card["plans"]:
        rows = []
        for r in p["rules"]:
            ms = r.get("merchants", [])
            scope = "全通路" if r.get("any_merchant") else "、".join(r.get("cats", []) + ms)
            if kw and kw.lower() not in scope.lower():
                continue
            rows.append({"規則": r["name"], "回饋": f"{r['rate']:.1%}",
                         "支付": pay_text(" / ".join(r.get("pay", ["card"]))),
                         "上限": r.get("cap", "無"), "適用": scope})
        if rows:
            period = f"{p.get('start', card.get('start', ''))} ~ {p.get('end', card.get('end', ''))}"
            with st.expander(f"**{p['name']}**　{period}", expanded=bool(kw)):
                if p.get("note"):
                    st.caption(p["note"])
                st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    if card.get("source"):
        st.caption(f"資料來源：{card['source']}")

# ───────── 3. 待登錄 & 到期 ─────────
with tab_todo:
    st.subheader("📌 需登錄 / 領券的活動")
    reg = set(state.get("registered", []))
    camp = [{"已登錄": c["id"] in reg, "卡片": c["card"], "活動": c["title"],
             "怎麼登錄": c.get("how", ""), "截止": c.get("end"), "id": c["id"]}
            for c in db["campaigns"] if c.get("register")]
    st.dataframe(pd.DataFrame(camp).sort_values(["已登錄", "截止"]), hide_index=True,
                 width="stretch")
    st.caption("登錄後把 id 加到 data/my_state.yaml 的 registered，查詢時就會算進去。")

    st.subheader("⏰ 45 天內到期的權益（該去看新權益了）")
    exp = expiring(db)
    if exp:
        st.dataframe(pd.DataFrame(exp), hide_index=True, width="stretch",
                     column_config={"來源": st.column_config.LinkColumn()})
    else:
        st.write("近期沒有到期項目 👍")
    unverified = [c["name"] for c in db["cards"] if c.get("verify")]
    if unverified:
        st.warning("以下卡片規則來自第三方整理，尚未對照官網：" + "、".join(unverified))
