"""規則引擎邊界測試：使用假規則（tests/fixtures），不依賴真實卡片資料。"""
from datetime import date
from pathlib import Path

import pytest

from card_assistant.engine import expiring, load_db, recommend, record, resolve_merchant

FIX = Path(__file__).parent / "fixtures"
WED = date(2026, 10, 21)   # 週三
SAT = date(2026, 10, 24)   # 週六


@pytest.fixture
def db():
    return load_db(FIX)


def state(**kw):
    s = {"cards": ["flat", "switch", "capped"], "pays": ["linepay"], "registered": [], "usage": {}}
    s.update(kw)
    return s


def top(opts, card):
    return next(o for o in opts if o.card == card)


def test_alias_resolves_to_canonical(db):
    assert resolve_merchant(db, "小七") == ("7-ELEVEN", {"超商"})
    assert resolve_merchant(db, "某間小吃店")[0] == "某間小吃店"


def test_switch_card_suggests_right_plan(db):
    o = top(recommend(db, state(), "蝦皮", 1000, WED), "switch")
    assert o.plan == "網購方案" and o.reward == 30


def test_ranking_highest_first(db):
    opts = recommend(db, state(), "蝦皮", 1000, WED)
    assert [o.reward for o in opts] == sorted([o.reward for o in opts], reverse=True)


def test_cap_partially_used(db):
    # capped 卡 5%（加碼 4%），上限 20；已用 15 → 只剩 5
    o = top(recommend(db, state(usage={"capped": 15}), "momo", 1000, WED), "capped")
    assert o.reward == pytest.approx(1000 * 0.01 + 5)
    assert any("上限" in n for n in o.notes)


def test_cap_exhausted_falls_back_to_base(db):
    o = top(recommend(db, state(usage={"capped": 999}), "momo", 1000, WED), "capped")
    assert o.reward == pytest.approx(10)


def test_shared_cap_group(db):
    # 兩條規則共用 cap_group=capped，用另一條規則的店也會被扣到
    o = top(recommend(db, state(usage={"capped": 20}), "Netflix", 100, WED), "capped")
    assert o.reward == pytest.approx(1)


def test_expired_rule_ignored(db):
    o = top(recommend(db, state(), "過期店", 1000, WED), "flat")
    assert o.rule == "一般消費" and o.reward == 10


def test_rule_active_before_expiry(db):
    o = top(recommend(db, state(), "過期店", 1000, date(2026, 1, 1)), "flat")
    assert o.reward == 50


def test_payment_method_condition(db):
    opts = recommend(db, state(), "任何店", 1000, WED)
    lp = next(o for o in opts if o.card == "switch" and o.pay == "linepay")
    card = next(o for o in opts if o.card == "switch" and "card" in o.pay)
    assert lp.plan == "LINE Pay方案" and lp.reward == 20 and card.reward == 3


def test_unowned_pay_method_excluded(db):
    opts = recommend(db, state(pays=[]), "任何店", 1000, WED)
    assert all(o.pay == "card" for o in opts)


def test_multiple_conditions_weekend_and_min_amount(db):
    # 假日 + 單筆滿 500 才有 6%
    assert top(recommend(db, state(), "任何店", 1000, SAT), "flat").reward == 60
    assert top(recommend(db, state(), "任何店", 400, SAT), "flat").reward == 4
    assert top(recommend(db, state(), "任何店", 1000, WED), "flat").reward == 10


def test_card_exclusion(db):
    assert not any(o.card == "capped" for o in recommend(db, state(), "全聯", 500, WED))


def test_campaign_needs_registration(db):
    o = top(recommend(db, state(), "7-11", 100, WED), "flat")
    assert o.reward == 1 and any("未登錄" in n for n in o.notes)
    o = top(recommend(db, state(registered=["flat-711"]), "7-11", 100, WED), "flat")
    assert o.reward == pytest.approx(1 + 3)


def test_expiring_lists_soon_ending(db):
    names = [r["項目"] for r in expiring(db, WED, within_days=30)]
    assert "網購方案" in names


def test_category_match_and_overseas_expansion(db):
    o = top(recommend(db, state(), "東京", 1000, WED), "flat")
    assert o.rule == "海外" and o.reward == 20


def test_merchant_accepted_pays(db):
    # 全聯不收 LINE Pay → 不該出現 linepay 選項
    opts = recommend(db, state(), "全聯", 1000, WED)
    assert all("linepay" not in o.pay for o in opts)


def test_record_consumes_cap_then_next_query_sees_it(db):
    s = state()
    o = top(recommend(db, s, "momo", 300, WED), "capped")   # 加碼 4% = 12
    s = record(s, o, WED)
    assert s["usage"]["capped"] == 12
    o2 = top(recommend(db, s, "momo", 1000, WED), "capped")  # 剩 8
    assert o2.reward == pytest.approx(10 + 8)


def test_usage_resets_next_month(db):
    s = record(state(), top(recommend(db, state(), "momo", 500, WED), "capped"), WED)
    o = top(recommend(db, s, "momo", 500, date(2026, 11, 2)), "capped")
    assert o.reward == pytest.approx(5 + 20)
