"""시연용 합성 분봉 데이터 생성기.

실제 시세가 아니다. 장 초반 급등 후 이어지거나(지속) 밀리는(실패) 패턴을
섞어서, 돌파 매매 전략의 승패가 모두 나오도록 만든 연습용 데이터다.
같은 seed면 항상 같은 데이터가 나온다.
"""
from __future__ import annotations

import csv
import io
import random
from datetime import date, datetime, timedelta

from .engine import Bar

SYMBOLS = ["SMP001", "SMP002", "SMP003", "SMP004", "SMP005", "SMP006", "SMP007", "SMP008"]


def _trading_days(n: int, start: date = date(2026, 10, 12)) -> list[date]:
    days, d = [], start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def generate(days: int = 15, seed: int = 42) -> list[Bar]:
    rng = random.Random(seed)
    bars: list[Bar] = []
    for code in SYMBOLS:
        price = rng.uniform(5_000, 60_000)
        for d in _trading_days(days):
            price *= 1 + rng.gauss(0, 0.02)
            px = price * (1 + rng.gauss(0.002, 0.01))
            # 이 종목·이 날에 급등 이벤트가 있는가 (약 35%)
            burst = rng.random() < 0.35
            b_start = rng.randint(15, 70)                 # 09:15~10:10 사이 시작
            b_len = rng.randint(4, 18)
            b_drift = rng.uniform(0.0015, 0.0045)         # 분당 상승 강도
            follow = rng.random() < 0.30                  # 급등 뒤 이어질 확률
            base_vol = rng.uniform(2_000, 20_000)
            # 가짜 돌파(거래량 동반 반짝 상승 후 되밀림)가 있는가 (약 45%)
            fake = rng.random() < 0.45
            f_start = rng.randint(20, 110)
            for m in range(390):                          # 09:00 ~ 15:29
                ts = datetime(d.year, d.month, d.day, 9, 0) + timedelta(minutes=m)
                drift, vol_k, sigma = 0.0, 1.0, 0.0018
                if m < 30:
                    vol_k, sigma = 2.0, 0.0028           # 장 초반 변동성
                if fake and f_start <= m < f_start + 3:
                    drift, vol_k = 0.006, rng.uniform(3.0, 6.0)
                elif fake and f_start + 3 <= m < f_start + 25:
                    drift, vol_k = -0.0022, 1.8
                if burst and b_start <= m < b_start + b_len:
                    drift, vol_k, sigma = b_drift, rng.uniform(2.5, 6.0), 0.0030
                elif burst and b_start + b_len <= m < b_start + b_len + 60:
                    drift = 0.0007 if follow else -0.0016
                    vol_k = 1.6
                o = px
                c = o * (1 + drift + rng.gauss(0, sigma))
                hi = max(o, c) * (1 + abs(rng.gauss(0, sigma * 0.6)))
                lo = min(o, c) * (1 - abs(rng.gauss(0, sigma * 0.6)))
                v = base_vol * vol_k * rng.uniform(0.5, 1.5)
                bars.append(Bar(code, ts, round(o, 1), round(hi, 1), round(lo, 1), round(c, 1), round(v)))
                px = c
            price = px
    return bars


def to_csv(bars: list[Bar]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["code", "datetime", "open", "high", "low", "close", "volume"])
    for b in bars:
        w.writerow([b.code, b.ts.strftime("%Y-%m-%d %H:%M"), b.open, b.high, b.low, b.close, int(b.volume)])
    return buf.getvalue()
