"""優惠爬蟲解析測試：用仿卡優結構的假 HTML，不連網。"""
from datetime import date

from card_assistant.promos import extract, match_merchants, parse_detail, parse_list, parse_period, relevant

LIST_HTML = """
<h3><a target="_blank" href="/epoint/detail.php?1" title="x">不是優支付</a></h3>
<span class="mb-1">活動日期：即日起~2026-11-30</span>
<h3><a target="_blank" href="/mpay/detail.php?100" title="A">全聯 x 全支付　滿額 5% 回饋</a></h3>
<span class="mb-1">活動日期：即日起~2026-11-30</span><p>...</p>
<h3><a target="_blank" href="/mpay/detail.php?101" title="B">悠遊付 週五會員日</a></h3>
<span class="mb-1">活動日期：2026-11-01~2026-12-31</span><p>...</p>
"""

DETAIL_HTML = """<html><body><nav>選單</nav>
<h1>正官庄 x LINE Pay　單筆滿額享11%回饋</h1>
活動期間：即日起~2026-11-13<br>發布日期：2026-11-05<br>
<p>於正官庄官網透過LINE Pay使用已綁定的任一發卡銀行信用卡付款，單筆滿新台幣 5,000元，享 LINE POINTS 11% 回饋。(限量10萬點，每人上限 500 點)</p>
◎情報來源：<a href="https://official.example/promo">點此查看</a>
◎延伸閱讀 其他文章
</body></html>"""


def test_parse_period_variants():
    assert parse_period("即日起~2026-11-13") == (None, "2026-11-13")
    assert parse_period("2026/11/01 - 2027/01/31") == ("2026-11-01", "2027-01-31")
    assert parse_period("無") == (None, None)


def test_parse_list_skips_other_sections():
    items = parse_list(LIST_HTML)
    assert [i["id"] for i in items] == ["100", "101"]
    assert items[1]["start"] == "2026-11-01" and items[1]["end"] == "2026-12-31"


def test_parse_detail_body_and_source():
    d = parse_detail(DETAIL_HTML)
    assert d["end"] == "2026-11-13" and d["published"] == "2026-11-05"
    assert "正官庄官網" in d["body"] and "延伸閱讀" not in d["body"] and "選單" not in d["body"]
    assert d["source"] == "https://official.example/promo"


def test_extract_fields():
    d = parse_detail(DETAIL_HTML)
    e = extract("正官庄 x LINE Pay　單筆滿額享11%回饋", d["body"])
    assert e["pays"] == ["linepay"] and e["any_card"]
    assert e["max_rate"] == 0.11 and e["min_amount"] == 5000 and e["cap"] == 500
    assert e["limited"] and not e["register"]


def test_extract_bank_maps_to_my_cards():
    e = extract("國泰世華卡綁全支付", "登錄後享 3% 回饋")
    assert e["cards"] == ["cube"] and e["pays"] == ["pxpay_plus"] and e["register"]


def test_match_merchants_ignores_one_char_names():
    assert match_merchants("在全聯與7-ELEVEN消費", ["全聯", "7-ELEVEN", "全"]) == ["7-ELEVEN", "全聯"]


def test_relevant_filters_by_my_pays_cards_and_date():
    promos = [
        {"title": "A", "pays": ["linepay"], "cards": [], "any_card": False, "end": "2026-11-30", "merchants": ["全聯"]},
        {"title": "B", "pays": ["jkopay"], "cards": [], "any_card": False, "end": "2026-11-30", "merchants": []},
        {"title": "C", "pays": [], "cards": ["cube"], "any_card": False, "end": "2026-10-01", "merchants": []},
        {"title": "D", "pays": [], "cards": ["cube"], "any_card": False, "end": "2026-12-31", "merchants": []},
    ]
    st = {"pays": ["linepay"], "cards": ["cube"]}
    on = date(2026, 11, 6)
    assert [p["title"] for p in relevant(promos, st, on=on)] == ["A", "D"]
    assert [p["title"] for p in relevant(promos, st, "全聯", on)] == ["A"]


def test_title_first_and_bank_list_noise():
    # 標題寫悠遊卡（實體卡），內文順帶提到悠遊付 → 以標題為準
    assert extract("7-ELEVEN 嗶悠遊卡 滿500折50", "也可用悠遊付")["pays"] == ["easycard"]
    # 內文列出一堆銀行 → 不當成限定卡
    assert extract("公車自動加值上線", "適用台新、玉山、國泰、富邦、中國信託信用卡")["cards"] == []
    # 中國信託不重複算成「中信」
    assert extract("中國信託卡綁LINE Pay", "")["cards"] == ["uniopen"]
