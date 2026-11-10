"""단타 전략 검증 엔진.

- 비용 반영 순수익률 계산
- 손익분기 승률 계산
- 매매기록 성과 지표(승률, 기대수익, 손익비, MDD 등)
- 분봉 기반 진입 신호 탐지 + 청산 규칙 시뮬레이션
- 손절/익절 조합 그리드 비교

외부 라이브러리 없이 표준 라이브러리만 사용한다.
"""
from __future__ import annotations

import csv
import io
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from statistics import mean


# ---------------------------------------------------------------------------
# 비용 모델
# ---------------------------------------------------------------------------
@dataclass
class Costs:
    fee_pct: float = 0.015      # 매수·매도 각각 수수료 %
    sell_tax_pct: float = 0.20  # 매도 시 거래세 %
    slippage_pct: float = 0.10  # 매수·매도 각각 미끄러짐 %


def net_return_pct(entry: float, exit_: float, c: Costs) -> float:
    """진입가·청산가(관찰가격)로 비용을 모두 반영한 순수익률(%)."""
    if entry <= 0:
        raise ValueError("진입가는 0보다 커야 합니다.")
    buy = entry * (1 + c.slippage_pct / 100) * (1 + c.fee_pct / 100)
    sell = exit_ * (1 - c.slippage_pct / 100) * (1 - (c.fee_pct + c.sell_tax_pct) / 100)
    return (sell / buy - 1) * 100


def breakeven(take_profit_pct: float, stop_loss_pct: float, c: Costs) -> dict:
    """익절/손절이 그대로 체결된다고 가정할 때 본전 승률."""
    if take_profit_pct <= 0 or stop_loss_pct <= 0:
        raise ValueError("익절·손절 %는 0보다 커야 합니다.")
    win = net_return_pct(100, 100 * (1 + take_profit_pct / 100), c)
    loss = net_return_pct(100, 100 * (1 - stop_loss_pct / 100), c)
    if win <= 0:
        return {"net_win_pct": round(win, 3), "net_loss_pct": round(loss, 3),
                "breakeven_win_rate_pct": None, "round_trip_cost_pct": round(-net_return_pct(100, 100, c), 3),
                "note": "익절 폭이 비용보다 작아서 이겨도 손해입니다."}
    p = -loss / (win - loss)
    return {
        "net_win_pct": round(win, 3),
        "net_loss_pct": round(loss, 3),
        "round_trip_cost_pct": round(-net_return_pct(100, 100, c), 3),
        "breakeven_win_rate_pct": round(p * 100, 2),
        "reward_risk_ratio": round(win / -loss, 3),
        "note": "익절·손절 가격에 정확히 체결된다는 가정입니다. 본전보호·시간손절이 섞이면 실제 필요 승률은 더 높아집니다.",
    }


# ---------------------------------------------------------------------------
# 성과 지표
# ---------------------------------------------------------------------------
def _num(v):
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def performance(trades: list[dict]) -> dict:
    """trades: net_return_pct 필수. reason / time / date / mfe_pct / mae_pct 선택."""
    rets = [t["net_return_pct"] for t in trades]
    n = len(rets)
    if n == 0:
        return {"trades": 0, "message": "분석할 거래가 없습니다."}
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r < 0]
    equity, peak, mdd, curve = 0.0, 0.0, 0.0, []
    for r in rets:
        equity += r
        peak = max(peak, equity)
        mdd = min(mdd, equity - peak)
        curve.append(round(equity, 3))

    by_reason = defaultdict(list)
    by_hour = defaultdict(list)
    for t in trades:
        by_reason[t.get("reason") or "미기재"].append(t["net_return_pct"])
        tm = str(t.get("entry_time") or t.get("time") or "")
        hh = tm[-8:-6] if len(tm) >= 8 and tm[-6] == ":" else (tm[:2] if len(tm) >= 5 and tm[2] == ":" else "")
        if hh.isdigit():
            by_hour[f"{hh}시"].append(t["net_return_pct"])

    def group(d):
        return sorted(
            ({"group": k, "trades": len(v), "win_rate_pct": round(100 * sum(x > 0 for x in v) / len(v), 1),
              "avg_return_pct": round(mean(v), 3), "sum_return_pct": round(sum(v), 3)} for k, v in d.items()),
            key=lambda x: -x["trades"])

    mfe = [t["mfe_pct"] for t in trades if t.get("mfe_pct") is not None]
    mae = [t["mae_pct"] for t in trades if t.get("mae_pct") is not None]
    avg_win = mean(wins) if wins else 0.0
    avg_loss = mean(losses) if losses else 0.0
    need = (-avg_loss / (avg_win - avg_loss) * 100) if wins and losses else None
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(100 * len(wins) / n, 2),
        "avg_win_pct": round(avg_win, 3),
        "avg_loss_pct": round(avg_loss, 3),
        "expectancy_pct": round(mean(rets), 3),
        "profit_factor": round(sum(wins) / -sum(losses), 3) if losses else None,
        "sum_return_pct": round(sum(rets), 3),
        "max_drawdown_pct": round(mdd, 3),
        "breakeven_win_rate_from_avgs_pct": round(need, 2) if need is not None else None,
        "avg_mfe_pct": round(mean(mfe), 3) if mfe else None,
        "avg_mae_pct": round(mean(mae), 3) if mae else None,
        "equity_curve": curve,
        "by_reason": group(by_reason),
        "by_hour": sorted(group(by_hour), key=lambda x: x["group"]),
        "verdict": verdict(n, mean(rets), sum(wins) / -sum(losses) if losses else None),
    }


def verdict(n: int, expectancy: float, pf: float | None) -> str:
    if n < 30:
        sample = f"거래 {n}건은 표본이 작아 결론을 내기 어렵습니다(최소 30건, 권장 100건 이상). "
    else:
        sample = ""
    if expectancy > 0 and (pf is None or pf >= 1.3):
        body = "비용 반영 후 기대수익이 플러스입니다. 기간을 늘려 같은 결과가 반복되는지 확인하세요."
    elif expectancy > 0:
        body = "기대수익은 플러스지만 손익비 여유가 작아 실전 미끄러짐에 취약합니다."
    else:
        body = "비용 반영 후 기대수익이 마이너스입니다. 이 설정 그대로 실전에 쓰면 손실이 누적될 가능성이 큽니다."
    return sample + body


def parse_trades_csv(text: str) -> list[dict]:
    """한투 단타 프로그램의 trades_v12.csv 형식 또는 일반 형식을 읽는다.

    필수: net_return_pct (또는 return_pct / 수익률)
    """
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not reader.fieldnames:
        raise ValueError("CSV 머리글이 없습니다.")
    keys = {k.strip(): k for k in reader.fieldnames}
    ret_key = next((keys[k] for k in ("net_return_pct", "return_pct", "수익률") if k in keys), None)
    if ret_key is None:
        raise ValueError("net_return_pct(순수익률 %) 열이 필요합니다.")
    out = []
    for row in reader:
        if keys.get("side") and str(row.get(keys["side"], "")).upper() not in ("", "SELL"):
            continue
        r = _num(row.get(ret_key))
        if r is None:
            continue
        out.append({
            "net_return_pct": r,
            "reason": (row.get(keys.get("reason", ""), "") or "").strip() or None,
            "time": (row.get(keys.get("time", ""), "") or "").strip(),
            "date": (row.get(keys.get("date", ""), "") or "").strip(),
            "mfe_pct": _num(row.get(keys.get("mfe_pct", ""))),
            "mae_pct": _num(row.get(keys.get("mae_pct", ""))),
        })
    return out


# ---------------------------------------------------------------------------
# 분봉 시뮬레이션
# ---------------------------------------------------------------------------
@dataclass
class Bar:
    code: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Rules:
    stop_loss_pct: float = 2.0
    take_profit_pct: float = 5.0
    breakeven_trigger_pct: float = 2.5   # 0이면 사용 안 함
    breakeven_offset_pct: float = 0.5
    trail_trigger_pct: float = 4.0       # 0이면 사용 안 함
    trail_pct: float = 1.5
    time_stop_minutes: int = 40          # 0이면 사용 안 함
    time_stop_min_gain_pct: float = 1.0
    force_exit: str = "15:15"
    # 진입 규칙 (원본 프로그램의 핵심 조건을 분봉으로 단순화)
    entry_start: str = "09:15"
    entry_end: str = "11:00"
    min_change_pct: float = 2.0          # 당일 시가 대비
    max_change_pct: float = 15.0
    min_volume_ratio: float = 1.5        # 직전 분봉 거래량 / 앞 5개 평균
    max_vwap_gap_pct: float = 3.0
    max_trades_per_day: int = 4
    costs: Costs = field(default_factory=Costs)


def _hm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def parse_bars_csv(text: str) -> list[Bar]:
    """열: code, datetime(YYYY-MM-DD HH:MM), open, high, low, close, volume"""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    need = {"code", "datetime", "open", "high", "low", "close", "volume"}
    if not reader.fieldnames or not need <= {f.strip() for f in reader.fieldnames}:
        raise ValueError("분봉 CSV 열은 code, datetime, open, high, low, close, volume 이어야 합니다.")
    bars = []
    for i, row in enumerate(reader, start=2):
        row = {k.strip(): v for k, v in row.items()}
        try:
            ts = datetime.fromisoformat(row["datetime"].strip().replace("/", "-"))
            vals = [float(row[k]) for k in ("open", "high", "low", "close", "volume")]
        except (ValueError, AttributeError):
            raise ValueError(f"{i}번째 줄 형식 오류") from None
        o, h, l, c, v = vals
        if min(o, h, l, c) <= 0 or h < max(o, c, l) or l > min(o, c, h):
            raise ValueError(f"{i}번째 줄 가격이 맞지 않습니다(고가/저가 확인).")
        bars.append(Bar(row["code"].strip(), ts, o, h, l, c, v))
    if not bars:
        raise ValueError("분봉 데이터가 비어 있습니다.")
    return bars


def _group_days(bars: list[Bar]) -> dict[tuple[str, str], list[Bar]]:
    g = defaultdict(list)
    for b in bars:
        g[(b.code, b.ts.date().isoformat())].append(b)
    for v in g.values():
        v.sort(key=lambda b: b.ts)
    return g


def find_entries(day: list[Bar], r: Rules) -> list[int]:
    """당일 고가 돌파 + 거래량 급증 + VWAP 근접 + 등락률 범위. 진입 봉 인덱스(종가 진입)."""
    out = []
    day_open = day[0].open
    run_high = day[0].high
    pv = vol = 0.0
    start, end = _hm(r.entry_start), _hm(r.entry_end)
    for i, b in enumerate(day):
        typical = (b.high + b.low + b.close) / 3
        pv += typical * b.volume
        vol += b.volume
        vwap = pv / vol if vol else b.close
        minute = b.ts.hour * 60 + b.ts.minute
        if i >= 5 and start <= minute <= end:
            base = mean(x.volume for x in day[i - 5:i])
            ratio = b.volume / base if base > 0 else 0
            change = (b.close / day_open - 1) * 100
            if (b.close > run_high and ratio >= r.min_volume_ratio
                    and r.min_change_pct <= change <= r.max_change_pct
                    and vwap <= b.close <= vwap * (1 + r.max_vwap_gap_pct / 100)):
                out.append(i)
                break  # 종목당 하루 1회
        run_high = max(run_high, b.high)
    return out


def simulate_exit(day: list[Bar], i: int, r: Rules) -> dict:
    """i번 봉 종가에 진입했다고 보고 이후 분봉으로 청산을 재현.

    한 봉 안에서 고가·저가 중 무엇이 먼저인지 모르므로 손절을 먼저 본다(보수적).
    갭으로 손절가 아래에서 시작하면 시가에 청산한다.
    """
    entry = day[i].close
    peak = trough = entry
    force = _hm(r.force_exit)
    exit_px, reason, exit_bar = day[-1].close, "데이터 끝", day[-1]
    for b in day[i + 1:]:
        stop, why = entry * (1 - r.stop_loss_pct / 100), "손절"
        if r.breakeven_trigger_pct and peak >= entry * (1 + r.breakeven_trigger_pct / 100):
            stop, why = entry * (1 + r.breakeven_offset_pct / 100), "본전보호"
        if r.trail_trigger_pct and peak >= entry * (1 + r.trail_trigger_pct / 100):
            t = peak * (1 - r.trail_pct / 100)
            if t > stop:
                stop, why = t, "추적스톱"
        minute = b.ts.hour * 60 + b.ts.minute
        if minute >= force:
            exit_px, reason, exit_bar = b.open, "마감청산", b
            break
        if b.low <= stop:
            exit_px, reason, exit_bar = min(stop, b.open), why, b
            trough = min(trough, b.low)
            break
        tp = entry * (1 + r.take_profit_pct / 100)
        if b.high >= tp:
            exit_px, reason, exit_bar = max(tp, b.open), "익절", b
            peak = max(peak, b.high)
            break
        peak, trough = max(peak, b.high), min(trough, b.low)
        held = (b.ts - day[i].ts).total_seconds() / 60
        if r.time_stop_minutes and held >= r.time_stop_minutes and peak < entry * (1 + r.time_stop_min_gain_pct / 100):
            exit_px, reason, exit_bar = b.close, "시간손절", b
            break
    return {
        "code": day[i].code,
        "date": day[i].ts.date().isoformat(),
        "entry_time": day[i].ts.strftime("%H:%M"),
        "exit_time": exit_bar.ts.strftime("%H:%M"),
        "entry_price": round(entry, 2),
        "exit_price": round(exit_px, 2),
        "reason": reason,
        "gross_return_pct": round((exit_px / entry - 1) * 100, 3),
        "net_return_pct": round(net_return_pct(entry, exit_px, r.costs), 3),
        "mfe_pct": round((peak / entry - 1) * 100, 3),
        "mae_pct": round((trough / entry - 1) * 100, 3),
        "hold_min": round((exit_bar.ts - day[i].ts).total_seconds() / 60, 1),
    }


def simulate(bars: list[Bar], r: Rules) -> dict:
    days = _group_days(bars)
    trades = []
    per_date = defaultdict(int)
    candidates = []
    for (code, d), day in days.items():
        for i in find_entries(day, r):
            candidates.append((day[i].ts, code, d, i))
    for ts, code, d, i in sorted(candidates):
        if per_date[d] >= r.max_trades_per_day:
            continue
        per_date[d] += 1
        trades.append(simulate_exit(days[(code, d)], i, r))
    trades.sort(key=lambda t: (t["date"], t["entry_time"], t["code"]))
    return {
        "days": len({d for _, d in days}),
        "symbols": len({c for c, _ in days}),
        "performance": performance(trades),
        "trades": trades,
        "rules": asdict(r),
    }


def grid(bars: list[Bar], base: Rules, stops: list[float], targets: list[float],
         breakeven_options: tuple[bool, ...] = (True, False)) -> list[dict]:
    if len(stops) * len(targets) * len(breakeven_options) > 200:
        raise ValueError("조합이 너무 많습니다(최대 200개).")
    rows = []
    for sl in stops:
        for tp in targets:
            for be in breakeven_options:
                r = Rules(**{**asdict(base), "costs": base.costs})
                r.stop_loss_pct, r.take_profit_pct = sl, tp
                if not be:
                    r.breakeven_trigger_pct = 0
                p = simulate(bars, r)["performance"]
                rows.append({
                    "stop_loss_pct": sl, "take_profit_pct": tp, "breakeven": be,
                    "trades": p.get("trades", 0), "win_rate_pct": p.get("win_rate_pct"),
                    "expectancy_pct": p.get("expectancy_pct"), "profit_factor": p.get("profit_factor"),
                    "sum_return_pct": p.get("sum_return_pct"), "max_drawdown_pct": p.get("max_drawdown_pct"),
                })
    rows.sort(key=lambda x: (x["expectancy_pct"] is None, -(x["expectancy_pct"] or 0)))
    for rank, row in enumerate(rows, 1):
        row["rank"] = rank
    return rows
