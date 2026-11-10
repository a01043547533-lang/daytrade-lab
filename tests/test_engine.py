from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import engine, sample
from app.main import app

client = TestClient(app)
NO_COST = engine.Costs(0, 0, 0)


def bars_from_closes(closes, start="2026-11-02 09:00", code="TEST01", vol=None):
    t0 = datetime.fromisoformat(start)
    out = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        v = vol[i] if vol else 1000
        out.append(engine.Bar(code, t0 + timedelta(minutes=i), o, max(o, c), min(o, c), c, v))
    return out


# ---- 비용/손익분기 ----
def test_net_return_without_cost_is_gross():
    assert engine.net_return_pct(100, 105, NO_COST) == pytest.approx(5.0)


def test_round_trip_cost_default():
    # 수수료 0.015x2 + 세금 0.2 + 미끄러짐 0.1x2 ≈ 0.43%
    assert -engine.net_return_pct(100, 100, engine.Costs()) == pytest.approx(0.429, abs=0.002)


def test_breakeven_symmetric_no_cost():
    assert engine.breakeven(2, 2, NO_COST)["breakeven_win_rate_pct"] == pytest.approx(50.0)


def test_breakeven_default_strategy():
    r = engine.breakeven(5, 2, engine.Costs())
    assert 34 < r["breakeven_win_rate_pct"] < 36


def test_breakeven_tp_smaller_than_cost():
    r = engine.breakeven(0.2, 2, engine.Costs())
    assert r["breakeven_win_rate_pct"] is None


# ---- 성과 지표 ----
def test_performance_metrics():
    p = engine.performance([{"net_return_pct": x} for x in (2, -1, 3, -1)])
    assert p["win_rate_pct"] == 50
    assert p["expectancy_pct"] == pytest.approx(0.75)
    assert p["profit_factor"] == pytest.approx(2.5)
    assert p["max_drawdown_pct"] == pytest.approx(-1.0)
    assert p["equity_curve"] == [2, 1, 4, 3]


def test_parse_trades_csv_kis_format():
    text = ("date,time,code,side,reason,pnl_est,net_return_pct,mfe_pct,mae_pct\n"
            "2026-11-05,09:31:02,005930,SELL,익절,1000,4.5,5.1,-0.3\n"
            "2026-11-05,10:02:11,000660,SELL,손절,-500,-2.4,0.4,-2.2\n")
    t = engine.parse_trades_csv(text)
    assert len(t) == 2 and t[0]["reason"] == "익절" and t[1]["mae_pct"] == -2.2
    assert engine.performance(t)["by_hour"][0]["group"] == "09시"


def test_parse_trades_csv_requires_return_column():
    with pytest.raises(ValueError):
        engine.parse_trades_csv("a,b\n1,2\n")


# ---- 청산 시뮬레이션 ----
def rules(**kw):
    base = dict(breakeven_trigger_pct=0, trail_trigger_pct=0, time_stop_minutes=0, costs=NO_COST)
    base.update(kw)
    return engine.Rules(**base)


def test_take_profit_hit():
    day = bars_from_closes([100, 101, 103, 106])
    r = engine.simulate_exit(day, 0, rules(take_profit_pct=5))
    assert r["reason"] == "익절" and r["exit_price"] == pytest.approx(105)


def test_stop_checked_before_tp_on_same_bar():
    day = bars_from_closes([100, 100])
    day[1] = engine.Bar("TEST01", day[1].ts, 100, 106, 97, 100, 1000)
    r = engine.simulate_exit(day, 0, rules(stop_loss_pct=2, take_profit_pct=5))
    assert r["reason"] == "손절"


def test_gap_down_exits_at_open():
    day = bars_from_closes([100, 100])
    day[1] = engine.Bar("TEST01", day[1].ts, 95, 96, 94, 95, 1000)
    r = engine.simulate_exit(day, 0, rules(stop_loss_pct=2))
    assert r["exit_price"] == pytest.approx(95)


def test_breakeven_protection():
    day = bars_from_closes([100, 103, 102, 100.2])
    r = engine.simulate_exit(day, 0, rules(breakeven_trigger_pct=2.5, breakeven_offset_pct=0.5, take_profit_pct=5))
    assert r["reason"] == "본전보호" and r["exit_price"] == pytest.approx(100.5)


def test_time_stop():
    day = bars_from_closes([100] + [100.2] * 50)
    r = engine.simulate_exit(day, 0, rules(time_stop_minutes=40, time_stop_min_gain_pct=1))
    assert r["reason"] == "시간손절" and r["hold_min"] == 40


def test_force_exit():
    day = bars_from_closes([100] * 10, start="2026-11-02 15:10")
    r = engine.simulate_exit(day, 0, rules())
    assert r["reason"] == "마감청산" and r["exit_time"] == "15:15"


def test_entry_detection_breakout_with_volume():
    closes = [100] * 20 + [101, 102.5, 103]
    vol = [1000] * 21 + [5000, 1000]
    day = bars_from_closes(closes, vol=vol)
    idx = engine.find_entries(day, engine.Rules(min_change_pct=2))
    assert idx == [21]


def test_parse_bars_rejects_bad_high_low():
    with pytest.raises(ValueError):
        engine.parse_bars_csv("code,datetime,open,high,low,close,volume\nA,2026-11-02 09:00,100,99,98,100,10\n")


def test_sample_is_deterministic():
    a, b = sample.generate(days=2), sample.generate(days=2)
    assert a[100] == b[100]


# ---- API ----
def test_api_health():
    assert client.get("/api/health").json()["status"] == "ok"


def test_api_index_and_docs():
    assert "단타 전략 검증기" in client.get("/").text
    assert client.get("/docs").status_code == 200


def test_api_breakeven():
    r = client.post("/api/breakeven", json={"take_profit_pct": 5, "stop_loss_pct": 2})
    assert r.status_code == 200 and 34 < r.json()["breakeven_win_rate_pct"] < 36


def test_api_analyze_sample():
    j = client.post("/api/analyze").json()
    assert j["performance"]["trades"] > 0 and "합성" in j["source"]


def test_api_analyze_upload():
    csv_text = "net_return_pct,reason\n1.5,익절\n-2,손절\n"
    j = client.post("/api/analyze", files={"file": ("t.csv", csv_text.encode("cp949"), "text/csv")}).json()
    assert j["performance"]["trades"] == 2


def test_api_simulate_and_grid():
    j = client.post("/api/simulate", data={"rules": '{"stop_loss_pct": 1.5}'}).json()
    assert j["rules"]["stop_loss_pct"] == 1.5 and j["performance"]["trades"] > 0
    g = client.post("/api/grid", data={"config": '{"stop_loss_list":[1.5,2],"take_profit_list":[3,5]}'}).json()
    assert len(g["results"]) == 8 and g["results"][0]["rank"] == 1


def test_api_bad_bars_upload():
    r = client.post("/api/simulate", files={"file": ("b.csv", b"x,y\n1,2\n", "text/csv")})
    assert r.status_code == 400


def test_api_sample_csv():
    r = client.get("/api/sample/bars.csv")
    assert r.text.startswith("code,datetime,open,high,low,close,volume")
