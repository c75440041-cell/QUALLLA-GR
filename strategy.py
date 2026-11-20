"""
Qullamaggie-inspired crypto scanner.
- Leaders: top momentum 7d/30d
- LONG: prior leg + tight base surfing EMA10/20 + breakout + RVOL
- SHORT: parabolic extension + crack (failed high / bearish close)
- Extra: volatility squeeze break
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np


@dataclass
class Signal:
    symbol: str
    side: str
    setup: str
    score: int
    entry: float
    sl: float
    tp: float
    tp_r: float
    mom_s: float
    mom_l: float
    rvol: float
    base_w: float
    note: str
    btc_regime: str = ""
    tf: str = "15m"


def _ema(x: np.ndarray, n: int) -> np.ndarray:
    out = np.empty_like(x, dtype=float)
    if len(x) == 0:
        return out
    k = 2 / (n + 1)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = x[i] * k + out[i - 1] * (1 - k)
    return out


def _atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, n: int = 14) -> float:
    if len(c) < n + 1:
        return 0.0
    prev = c[:-1]
    tr = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - prev), np.abs(l[1:] - prev)))
    return float(np.mean(tr[-n:])) if len(tr) >= n else float(np.mean(tr)) if len(tr) else 0.0


def _rvol(v: np.ndarray, n: int = 20) -> float:
    if len(v) < n + 1:
        return 0.0
    base = np.mean(v[-n - 1 : -1])
    return float(v[-1] / base) if base > 0 else 0.0


def _ret_pct(c: np.ndarray, bars: int) -> float:
    if bars < 1:
        return 0.0
    if len(c) <= bars:
        bars = max(1, len(c) - 1)
    if bars < 1 or c[-bars - 1] == 0:
        return 0.0
    return 100.0 * (c[-1] / c[-bars - 1] - 1.0)


def momentum_rank(ohlcv: list[list], bars_s: int, bars_l: int) -> tuple[float, float]:
    if len(ohlcv) < max(bars_s, bars_l) + 2:
        return 0.0, 0.0
    c = np.array([x[4] for x in ohlcv], dtype=float)
    return _ret_pct(c, bars_s), _ret_pct(c, bars_l)


def btc_regime(ohlcv: list[list], ema_n: int = 50) -> str:
    if len(ohlcv) < ema_n + 5:
        return "unknown"
    c = np.array([x[4] for x in ohlcv], dtype=float)
    e = _ema(c, ema_n)
    slope = e[-1] - e[-5]
    if c[-1] > e[-1] and slope > 0:
        return "bull"
    if c[-1] < e[-1] and slope < 0:
        return "bear"
    return "range"


def detect_breakout_long(
    symbol: str,
    ohlcv_struct: list[list],
    ohlcv_entry: list[list],
    *,
    prior_min_pct: float,
    base_bars: int,
    base_max_width_atr: float,
    break_buf_atr: float,
    rvol_min: float,
    rvol_len: int,
    ema_fast: int,
    ema_slow: int,
    sl_atr: float,
    tp_r: float,
    mom_s: float,
    mom_l: float,
) -> Optional[Signal]:
    """Prior strength + tight base on EMA + breakout on entry TF."""
    if len(ohlcv_struct) < base_bars + 30 or len(ohlcv_entry) < 30:
        return None
    cs = np.array([x[4] for x in ohlcv_struct], dtype=float)
    hs = np.array([x[2] for x in ohlcv_struct], dtype=float)
    ls = np.array([x[3] for x in ohlcv_struct], dtype=float)
    atr_s = _atr(hs, ls, cs)
    if atr_s <= 0:
        return None

    # prior leg before base
    if len(cs) < base_bars + 20:
        return None
    prior = cs[-(base_bars + 20) : -base_bars]
    if len(prior) < 5 or prior[0] <= 0:
        return None
    prior_pct = 100.0 * (prior[-1] / prior[0] - 1.0)
    if prior_pct < prior_min_pct and mom_s < prior_min_pct:
        return None

    base_h = hs[-base_bars - 1 : -1]
    base_l = ls[-base_bars - 1 : -1]
    if len(base_h) < 3:
        return None
    width = float(np.max(base_h) - np.min(base_l))
    if width > base_max_width_atr * atr_s:
        return None

    e10 = _ema(cs, ema_fast)
    e20 = _ema(cs, ema_slow)
    if not (cs[-2] > e10[-2] > e20[-2] or cs[-3] > e10[-3] > e20[-3]):
        # allow slight tolerance: price above slow EMA
        if cs[-2] < e20[-2]:
            return None

    # entry TF breakout
    ce = np.array([x[4] for x in ohlcv_entry], dtype=float)
    he = np.array([x[2] for x in ohlcv_entry], dtype=float)
    le = np.array([x[3] for x in ohlcv_entry], dtype=float)
    ve = np.array([x[5] for x in ohlcv_entry], dtype=float)
    atr_e = _atr(he, le, ce)
    if atr_e <= 0:
        return None
    rvol = _rvol(ve, rvol_len)
    if rvol < rvol_min:
        return None

    box_hi = float(np.max(he[-base_bars - 1 : -1])) if len(he) > base_bars + 1 else float(np.max(base_h))
    trigger = box_hi + break_buf_atr * atr_e
    if ce[-1] < trigger and he[-1] < trigger:
        return None
    if ce[-1] < ce[-2] and he[-1] < trigger * 1.002:
        return None  # weak

    entry = float(ce[-1])
    sl = entry - sl_atr * atr_e
    risk = entry - sl
    if risk <= 0:
        return None
    tp = entry + tp_r * risk
    score = _score_long(mom_s, mom_l, prior_pct, width / atr_s, rvol)
    return Signal(
        symbol=symbol,
        side="LONG",
        setup="BREAKOUT",
        score=score,
        entry=entry,
        sl=sl,
        tp=tp,
        tp_r=tp_r,
        mom_s=mom_s,
        mom_l=mom_l,
        rvol=rvol,
        base_w=width / atr_s,
        note="Leader + base + break · stop LOD-style ATR",
    )


def detect_parabolic_short(
    symbol: str,
    ohlcv_struct: list[list],
    ohlcv_entry: list[list],
    *,
    para_up_days: int,
    para_ext_atr: float,
    para_min_mom: float,
    rvol_min: float,
    rvol_len: int,
    sl_atr: float,
    tp_r: float,
    mom_s: float,
    mom_l: float,
) -> Optional[Signal]:
    if len(ohlcv_struct) < 30 or len(ohlcv_entry) < 20:
        return None
    if mom_s < para_min_mom and mom_l < para_min_mom:
        return None
    cs = np.array([x[4] for x in ohlcv_struct], dtype=float)
    hs = np.array([x[2] for x in ohlcv_struct], dtype=float)
    ls = np.array([x[3] for x in ohlcv_struct], dtype=float)
    atr_s = _atr(hs, ls, cs)
    if atr_s <= 0:
        return None
    # consecutive up closes
    ups = 0
    for i in range(1, min(para_up_days + 3, len(cs))):
        if cs[-i] > cs[-i - 1]:
            ups += 1
        else:
            break
    if ups < para_up_days - 1:
        return None
    e20 = _ema(cs, 20)
    ext = (cs[-1] - e20[-1]) / atr_s
    if ext < para_ext_atr:
        return None

    ce = np.array([x[4] for x in ohlcv_entry], dtype=float)
    he = np.array([x[2] for x in ohlcv_entry], dtype=float)
    le = np.array([x[3] for x in ohlcv_entry], dtype=float)
    ve = np.array([x[5] for x in ohlcv_entry], dtype=float)
    atr_e = _atr(he, le, ce)
    rvol = _rvol(ve, rvol_len)
    # crack: bearish close or lower high
    cracked = ce[-1] < ce[-2] or (he[-1] < he[-2] and ce[-1] < (he[-1] + le[-1]) / 2)
    if not cracked:
        return None
    if rvol < max(1.0, rvol_min * 0.85):
        return None

    entry = float(ce[-1])
    sl = entry + sl_atr * atr_e
    risk = sl - entry
    if risk <= 0:
        return None
    tp = entry - tp_r * risk
    score = min(100, 40 + int(min(mom_s, 80) * 0.4) + int(min(ext, 6) * 5) + int(min(rvol, 3) * 8))
    return Signal(
        symbol=symbol,
        side="SHORT",
        setup="PARABOLIC",
        score=score,
        entry=entry,
        sl=sl,
        tp=tp,
        tp_r=tp_r,
        mom_s=mom_s,
        mom_l=mom_l,
        rvol=rvol,
        base_w=ext,
        note=f"Ext {ext:.1f} ATR sobre EMA20 · crack",
    )


def detect_squeeze_break(
    symbol: str,
    ohlcv_entry: list[list],
    *,
    bb_len: int,
    squeeze_pct: float,
    rvol_min: float,
    rvol_len: int,
    break_buf_atr: float,
    sl_atr: float,
    tp_r: float,
    mom_s: float,
    mom_l: float,
    allow_long: bool,
    allow_short: bool,
) -> Optional[Signal]:
    if len(ohlcv_entry) < bb_len + 30:
        return None
    c = np.array([x[4] for x in ohlcv_entry], dtype=float)
    h = np.array([x[2] for x in ohlcv_entry], dtype=float)
    l = np.array([x[3] for x in ohlcv_entry], dtype=float)
    v = np.array([x[5] for x in ohlcv_entry], dtype=float)
    atr = _atr(h, l, c)
    if atr <= 0:
        return None
    # BB width series
    widths = []
    for i in range(bb_len, len(c)):
        window = c[i - bb_len : i]
        mid = np.mean(window)
        std = np.std(window)
        widths.append((2 * 2.0 * std) / mid if mid else 0.0)
    if len(widths) < 40:
        return None
    w = np.array(widths)
    rank = 100.0 * np.sum(w[:-1] <= w[-1]) / len(w[:-1])
    # was compressed recently
    recent_min_rank = min(
        100.0 * np.sum(w[: j + 1] <= w[j]) / max(1, j + 1) for j in range(len(w) - 8, len(w) - 1)
    ) if len(w) > 10 else 100.0
    compressed = recent_min_rank <= squeeze_pct or rank <= squeeze_pct + 10
    if not compressed:
        return None
    rvol = _rvol(v, rvol_len)
    if rvol < rvol_min:
        return None
    box_hi = float(np.max(h[-6:-1]))
    box_lo = float(np.min(l[-6:-1]))
    up = c[-1] > box_hi + break_buf_atr * atr
    dn = c[-1] < box_lo - break_buf_atr * atr
    if up and allow_long:
        entry = float(c[-1])
        sl = entry - sl_atr * atr
        risk = entry - sl
        if risk <= 0:
            return None
        return Signal(
            symbol=symbol,
            side="LONG",
            setup="SQUEEZE",
            score=min(100, 55 + int(rvol * 10) + int(max(0, 30 - rank))),
            entry=entry,
            sl=sl,
            tp=entry + tp_r * risk,
            tp_r=tp_r,
            mom_s=mom_s,
            mom_l=mom_l,
            rvol=rvol,
            base_w=rank,
            note="Compresión BB → ruptura alcista",
        )
    if dn and allow_short:
        entry = float(c[-1])
        sl = entry + sl_atr * atr
        risk = sl - entry
        if risk <= 0:
            return None
        return Signal(
            symbol=symbol,
            side="SHORT",
            setup="SQUEEZE",
            score=min(100, 55 + int(rvol * 10) + int(max(0, 30 - rank))),
            entry=entry,
            sl=sl,
            tp=entry - tp_r * risk,
            tp_r=tp_r,
            mom_s=mom_s,
            mom_l=mom_l,
            rvol=rvol,
            base_w=rank,
            note="Compresión BB → ruptura bajista",
        )
    return None


def _score_long(mom_s: float, mom_l: float, prior: float, width_atr: float, rvol: float) -> int:
    s = 40
    s += min(25, int(max(mom_s, prior) * 0.35))
    s += min(10, int(mom_l * 0.08))
    s += 10 if width_atr <= 1.5 else (5 if width_atr <= 2.5 else 0)
    s += min(15, int(rvol * 5))
    return int(max(0, min(100, s)))
