"""
Qulla Explosion Bot — BingX USDT-M perps
Escanea líderes (momentum), detecta BREAKOUT / PARABOLIC / SQUEEZE.
MODE=SIGNAL por defecto (Telegram). LIVE opcional con keys.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bingx import BingX
from config import settings
from notify import format_heartbeat, format_signal, send_telegram
from strategy import (
    Signal,
    btc_regime,
    detect_breakout_long,
    detect_parabolic_short,
    detect_squeeze_break,
    momentum_rank,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bot")


def _utc_hour() -> int:
    return datetime.now(timezone.utc).hour


def _day_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _in_session() -> bool:
    h = _utc_hour()
    return settings.session_start <= h < settings.session_end


def _load(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(path: str, data: dict) -> None:
    """Guarda JSON de forma atómica. Si /data no es escribible solo avisa (no tumba el bot)."""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(p)
    except Exception as e:
        log.warning("no pude guardar %s: %s", path, e)


class State:
    def __init__(self) -> None:
        self.data = _load(settings.state_path)
        self._roll()

    def _roll(self) -> None:
        d = _day_key()
        if self.data.get("day") != d:
            self.data = {
                "day": d,
                "signals_today": 0,
                "seen": {},
                "loss_streak": 0,
            }
            self.save()

    def save(self) -> None:
        _save(settings.state_path, self.data)

    def key(self, symbol: str, side: str) -> str:
        return f"{symbol}:{side}:{_day_key()}"

    def already(self, symbol: str, side: str) -> bool:
        return self.key(symbol, side) in self.data.get("seen", {})

    def mark(self, symbol: str, side: str) -> None:
        self.data.setdefault("seen", {})[self.key(symbol, side)] = time.time()
        self.data["signals_today"] = int(self.data.get("signals_today", 0)) + 1
        self.save()


class OICache:
    """Histórico de OI por símbolo ({sym: [[ts, oi], ...]}) para calcular Δ% en una ventana real.

    Antes solo se comparaba contra el ciclo anterior (~3 min), que es ruido puro; los umbrales
    del filtro (+8% / -12%) están pensados para horas, no para minutos.
    """

    SPACING_S = 600  # una muestra cada ≥10 min por símbolo

    def __init__(self, path: str, lookback_min: int):
        self.path = path
        self.lookback_s = lookback_min * 60
        self.keep_s = max(8 * 3600, int(self.lookback_s * 1.5))
        self.min_age_s = 3600  # no calcular Δ con muestras de <1h
        self.data: dict[str, list[list[float]]] = {}
        self._load()

    def _load(self) -> None:
        raw = _load(self.path)
        for sym, hist in raw.items():
            # formato viejo ({sym: float}) o corrupto → se descarta
            if not isinstance(hist, list):
                continue
            clean = [
                [float(h[0]), float(h[1])]
                for h in hist
                if isinstance(h, (list, tuple)) and len(h) == 2
            ]
            if clean:
                self.data[sym] = clean

    def save(self) -> None:
        _save(self.path, self.data)

    def delta_pct(self, sym: str, oi: float | None, now: float) -> float | None:
        """Δ% del OI actual vs la muestra más cercana a `lookback` (con edad ≥ 1h)."""
        if oi is None:
            return None
        old = [h for h in self.data.get(sym, []) if now - h[0] >= self.min_age_s]
        if not old:
            return None
        target = now - self.lookback_s
        ref = min(old, key=lambda h: abs(h[0] - target))
        if ref[1] <= 0:
            return None
        return 100.0 * (oi - ref[1]) / ref[1]

    def record(self, sym: str, oi: float | None, now: float) -> None:
        if oi is None or oi <= 0:
            return
        hist = self.data.setdefault(sym, [])
        if not hist or now - hist[-1][0] >= self.SPACING_S:
            hist.append([now, oi])
        cutoff = now - self.keep_s
        self.data[sym] = [h for h in hist if h[0] >= cutoff]

    def prune(self, now: float) -> None:
        cutoff = now - self.keep_s
        self.data = {s: h for s, h in self.data.items() if h and h[-1][0] >= cutoff}


def _ticker_vol(t: dict) -> float:
    for k in (
        "quoteVolume",
        "quoteVolume24h",
        "turnover",
        "turnover24h",
        "volume24h",
        "volume",
        "vol",
        "amount",
    ):
        v = t.get(k)
        if v is None or v == "":
            continue
        try:
            f = float(v)
            if f > 0:
                return f
        except (TypeError, ValueError):
            continue
    return 0.0


def _ticker_symbol(t: dict) -> str:
    for k in ("symbol", "pair", "s"):
        v = t.get(k)
        if v:
            return str(v).upper().replace("_", "-").replace("/", "-")
    return ""


async def universe(bx: BingX) -> list[str]:
    tickers = await bx.swap_tickers()
    if not tickers:
        log.warning("ticker list vacía")
        return []

    q = (settings.quote or "USDT").upper()
    max_n = max(20, int(settings.max_symbols or 180))
    min_vol = float(settings.min_quote_vol_24h or 0)

    if not getattr(universe, "_logged_sample", False):
        sample = tickers[0] if isinstance(tickers[0], dict) else {}
        log.info(
            "ticker sample keys=%s symbol=%s vol=%s | min_vol=%s max_n=%s quote=%s n_raw=%s",
            list(sample.keys())[:20] if isinstance(sample, dict) else sample,
            _ticker_symbol(sample) if isinstance(sample, dict) else "",
            _ticker_vol(sample) if isinstance(sample, dict) else 0,
            min_vol,
            max_n,
            q,
            len(tickers),
        )
        universe._logged_sample = True  # type: ignore

    rows: list[tuple[str, float]] = []
    for t in tickers:
        if not isinstance(t, dict):
            continue
        sym = _ticker_symbol(t)
        if not sym:
            continue
        if sym.endswith(q) and not sym.endswith(f"-{q}"):
            sym = f"{sym[:-len(q)]}-{q}"
        if not sym.endswith(f"-{q}"):
            continue
        rows.append((sym, _ticker_vol(t)))

    if not rows:
        log.warning("ningún símbolo %s en %s tickers", q, len(tickers))
        return []

    # filtrar por volumen solo si hay suficientes con vol > min
    with_vol = [r for r in rows if r[1] >= min_vol]
    use = with_vol if len(with_vol) >= 10 else rows
    use.sort(key=lambda x: x[1], reverse=True)
    out = [s for s, _ in use[:max_n]]
    log.info(
        "universo n=%s (raw=%s usdt=%s above_min_vol=%s min_vol=%s)",
        len(out),
        len(tickers),
        len(rows),
        len(with_vol),
        min_vol,
    )
    return out


def apply_funding_filter(
    sig: Signal,
    last_fr: float | None,
    avg_fr: float | None = None,
    oi: float | None = None,
    oi_chg_pct: float | None = None,
) -> Signal | None:
    """Bloquea/puntúa con funding (last + media ~7d) y Δ% de OI. None = señal descartada."""
    sig.funding = last_fr
    sig.funding_avg = avg_fr
    sig.oi = oi
    sig.oi_chg_pct = oi_chg_pct

    if not settings.use_funding_filter:
        return sig

    fr = avg_fr if (settings.use_funding_avg and avg_fr is not None) else last_fr
    if fr is None:
        sig.note = (sig.note + " · fund n/a").strip(" ·")
        return sig

    bps = fr * 10_000
    use_oi = settings.use_oi_filter and oi_chg_pct is not None

    if sig.side == "LONG":
        # largos saturados: funding medio alto, o último print en crowding extremo
        if fr > settings.funding_long_max:
            return None
        if last_fr is not None and last_fr > settings.funding_extreme:
            return None
        # OI subiendo + funding positivo = más frágil
        if use_oi and oi_chg_pct > 8 and fr > 0:
            sig.score = max(0, sig.score - 8)
            sig.note = (sig.note + f" · OI+{oi_chg_pct:.0f}% crowded").strip(" ·")
        # funding negativo (cortos pagan) = combustible para el breakout
        if fr <= -settings.funding_short_boost:
            sig.score = min(100, sig.score + 8)
            sig.note = (sig.note + f" · fundAvg {bps:.1f}bps ok+").strip(" ·")
        else:
            sig.note = (sig.note + f" · fundAvg {bps:.1f}bps").strip(" ·")
    else:  # SHORT
        # cortos saturados → riesgo de short squeeze
        if fr < settings.funding_short_min:
            return None
        if last_fr is not None and last_fr < -settings.funding_extreme:
            return None
        if fr >= settings.funding_short_boost:
            sig.score = min(100, sig.score + 10)
            sig.note = (sig.note + f" · fundAvg {bps:.1f}bps SHORT+").strip(" ·")
        elif fr >= 0:
            sig.score = min(100, sig.score + 4)
            sig.note = (sig.note + f" · fundAvg {bps:.1f}bps").strip(" ·")
        else:
            sig.note = (sig.note + f" · fundAvg {bps:.1f}bps").strip(" ·")
        # OI subiendo con funding + = más longs que liquidar en el crack
        if use_oi and oi_chg_pct > 5 and fr > 0:
            sig.score = min(100, sig.score + 6)
            sig.note = (sig.note + f" · OI+{oi_chg_pct:.0f}%").strip(" ·")
        # OI cayendo fuerte: ya hubo deleveraging
        if use_oi and oi_chg_pct < -12:
            sig.score = max(0, sig.score - 5)
            sig.note = (sig.note + f" · OI{oi_chg_pct:.0f}%").strip(" ·")

    return sig


async def scan_one(
    bx: BingX,
    symbol: str,
    btc_reg: str,
    kl_s: list[list],
    sem: asyncio.Semaphore,
) -> list[Signal]:
    """Devuelve TODOS los setups del símbolo (ordenados por score).

    kl_s son las velas de estructura ya descargadas en el ranking (antes se pedían dos veces).
    """
    async with sem:
        try:
            kl_e = await bx.klines(symbol, settings.tf_entry, min(120, settings.kline_limit))
        except Exception as e:
            log.debug("%s klines entry: %s", symbol, e)
            return []

    mom_s, mom_l = momentum_rank(kl_s, settings.mom_bars_short, settings.mom_bars_long)
    candidates: list[Signal] = []

    allow_l = settings.allow_long and (
        not settings.require_btc_align or btc_reg in ("bull", "range", "unknown")
    )
    allow_s = settings.allow_short and (
        not settings.require_btc_align or btc_reg in ("bear", "range", "unknown")
    )

    if allow_l:
        sig = detect_breakout_long(
            symbol,
            kl_s,
            kl_e,
            prior_min_pct=settings.prior_min_pct,
            base_bars=settings.base_bars,
            base_max_width_atr=settings.base_max_width_atr,
            break_buf_atr=settings.break_buf_atr,
            rvol_min=settings.rvol_min,
            rvol_len=settings.rvol_len,
            ema_fast=settings.ema_fast,
            ema_slow=settings.ema_slow,
            sl_atr=settings.sl_atr,
            tp_r=settings.tp_r,
            mom_s=mom_s,
            mom_l=mom_l,
        )
        if sig:
            candidates.append(sig)

    if allow_s:
        sig = detect_parabolic_short(
            symbol,
            kl_s,
            kl_e,
            para_up_days=settings.para_up_days,
            para_ext_atr=settings.para_ext_atr,
            para_min_mom=settings.para_min_mom,
            rvol_min=settings.rvol_min,
            rvol_len=settings.rvol_len,
            sl_atr=settings.sl_atr,
            tp_r=settings.tp_r,
            mom_s=mom_s,
            mom_l=mom_l,
        )
        if sig:
            candidates.append(sig)

    if settings.use_squeeze:
        sig = detect_squeeze_break(
            symbol,
            kl_e,
            bb_len=settings.bb_len,
            squeeze_pct=settings.squeeze_pct,
            rvol_min=settings.rvol_min,
            rvol_len=settings.rvol_len,
            break_buf_atr=settings.break_buf_atr,
            sl_atr=settings.sl_atr,
            tp_r=settings.tp_r,
            mom_s=mom_s,
            mom_l=mom_l,
            allow_long=allow_l,
            allow_short=allow_s,
        )
        if sig:
            candidates.append(sig)

    for c in candidates:
        c.btc_regime = btc_reg
        c.tf = settings.tf_entry
    candidates.sort(key=lambda x: x.score, reverse=True)
    return candidates


async def run_cycle(bx: BingX, state: State, oi_cache: OICache) -> dict[str, Any]:
    t0 = time.time()
    if not _in_session():
        return {"n_sym": 0, "cycle_s": 0, "skipped": "session", "hour": _utc_hour()}

    if state.data.get("signals_today", 0) >= settings.max_signals_day:
        return {"n_sym": 0, "cycle_s": 0, "skipped": "max_signals"}

    symbols = await universe(bx)
    if not symbols:
        return {"n_sym": 0, "cycle_s": time.time() - t0, "skipped": "no_universe"}

    try:
        btc_kl = await bx.klines(f"BTC-{settings.quote}", settings.tf_struct, 100)
        reg = btc_regime(btc_kl)
    except Exception:
        reg = "unknown"

    # 1) velas de estructura de todo el universo (se reutilizan en el scan)
    sem = asyncio.Semaphore(settings.workers)
    kl_struct: dict[str, list[list]] = {}

    async def _fetch_struct(sym: str) -> None:
        async with sem:
            try:
                kl = await bx.klines(sym, settings.tf_struct, settings.kline_limit)
            except Exception as e:
                log.debug("%s klines struct: %s", sym, e)
                return
        if kl and len(kl) >= 10:
            kl_struct[sym] = kl

    await asyncio.gather(*[_fetch_struct(s) for s in symbols])
    if not kl_struct:
        log.warning("klines vacías para todo el universo — API caída o rate limit")
        return {"n_sym": len(symbols), "cycle_s": time.time() - t0, "skipped": "no_klines"}

    # 2) ranking por |momentum| (incluye caídas fuertes para SHORT)
    mom_list: list[tuple[str, float, float]] = []
    for sym, kl in kl_struct.items():
        ms, ml = momentum_rank(kl, settings.mom_bars_short, settings.mom_bars_long)
        mom_list.append((sym, ms, ml))
    mom_list.sort(key=lambda x: (abs(x[1]), abs(x[2])), reverse=True)
    log.info(
        "momentum sample n=%s top=%s",
        len(mom_list),
        [(s, round(float(a), 1), round(float(b), 1)) for s, a, b in mom_list[:3]],
    )
    n_keep = max(30, int(len(symbols) * settings.top_pct))
    leaders = [x[0] for x in mom_list[:n_keep]]

    # 3) scan de leaders + snapshot de OI en paralelo
    async def _oi_now() -> dict[str, float]:
        if not settings.use_oi_filter:
            return {}
        try:
            return await bx.oi_map(leaders, workers=min(10, settings.workers))
        except Exception as e:
            log.warning("oi_map falló: %s", e)
            return {}

    scan_res, oi_now = await asyncio.gather(
        asyncio.gather(
            *[scan_one(bx, s, reg, kl_struct[s], sem) for s in leaders],
            return_exceptions=True,
        ),
        _oi_now(),
    )

    cand_by_sym: dict[str, list[Signal]] = {}
    for sym, r in zip(leaders, scan_res):
        if isinstance(r, BaseException):
            log.debug("scan %s: %r", sym, r)
            continue
        if r:
            cand_by_sym[sym] = r

    # 4) funding SOLO de los símbolos con setup (antes se pedía de todos los leaders)
    fund: dict[str, dict] = {}
    if settings.use_funding_filter and cand_by_sym:
        try:
            fund = await bx.funding_map(list(cand_by_sym), workers=min(8, settings.workers))
        except Exception as e:
            log.warning("funding_map falló: %s", e)
        log.info("funding n=%s/%s sample=%s", len(fund), len(cand_by_sym), list(fund.items())[:2])

    # 5) filtros: por símbolo se queda el mejor setup que sobreviva
    now = time.time()
    funnel = {
        "leaders": len(leaders),
        "candidates": sum(len(v) for v in cand_by_sym.values()),
        "fund_skip": 0,
        "setups": 0,
        "sent": 0,
    }
    signals: list[Signal] = []
    for sym, cands in cand_by_sym.items():
        info = fund.get(sym) or {}
        oi = oi_now.get(sym)
        oi_chg = oi_cache.delta_pct(sym, oi, now) if settings.use_oi_filter else None
        survivors: list[Signal] = []
        for c in cands:
            f = apply_funding_filter(c, info.get("last"), info.get("avg"), oi, oi_chg)
            if f is None:
                funnel["fund_skip"] += 1
            else:
                survivors.append(f)
        if survivors:
            survivors.sort(key=lambda x: x.score, reverse=True)
            signals.append(survivors[0])
            funnel["setups"] += 1
    signals.sort(key=lambda x: x.score, reverse=True)

    # snapshots de OI de todos los leaders (para tener histórico en próximos ciclos)
    if settings.use_oi_filter:
        for sym, oi in oi_now.items():
            oi_cache.record(sym, oi, now)
        oi_cache.prune(now)
        oi_cache.save()

    # 6) envío
    tg_configured = bool(settings.telegram_token and settings.telegram_chat_id)
    for sig in signals:
        if state.already(sig.symbol, sig.side):
            continue
        if state.data.get("signals_today", 0) >= settings.max_signals_day:
            break
        if sig.score < 50:
            continue
        payload = {
            "symbol": sig.symbol,
            "side": sig.side,
            "setup": sig.setup,
            "score": sig.score,
            "entry": round(sig.entry, 8),
            "sl": round(sig.sl, 8),
            "tp": round(sig.tp, 8),
            "tp_r": sig.tp_r,
            "mom_s": sig.mom_s,
            "mom_l": sig.mom_l,
            "rvol": sig.rvol,
            "base_w": round(sig.base_w, 2),
            "btc_regime": sig.btc_regime,
            "tf": sig.tf,
            "mode": settings.mode,
            "note": sig.note,
            "funding": None if sig.funding is None else round(sig.funding * 10000, 2),
            "funding_avg": None if sig.funding_avg is None else round(sig.funding_avg * 10000, 2),
            "oi_chg_pct": None if sig.oi_chg_pct is None else round(sig.oi_chg_pct, 1),
        }
        ok = await send_telegram(
            settings.telegram_token,
            settings.telegram_chat_id,
            format_signal(payload),
        )
        if not ok and tg_configured:
            # no se marca como vista: se reintenta en el próximo ciclo
            log.warning("Telegram falló para %s %s; reintento próximo ciclo", sig.symbol, sig.side)
            continue
        state.mark(sig.symbol, sig.side)
        funnel["sent"] += 1
        log.info("SIGNAL %s %s %s score=%s", sig.setup, sig.side, sig.symbol, sig.score)

        if settings.mode == "LIVE" and settings.bingx_api_key:
            log.warning("LIVE: implementa qty segura antes de enviar órdenes reales")

    return {
        "n_sym": len(symbols),
        "n_leaders": len(leaders),
        "cycle_s": time.time() - t0,
        "signals_today": state.data.get("signals_today", 0),
        "mode": settings.mode,
        "session": f"{settings.session_start}-{settings.session_end}",
        "btc_regime": reg,
        "funnel": funnel,
    }


async def main() -> None:
    log.info(
        "Qulla Explosion MODE=%s TF=%s/%s TOP=%.0f%% session=%s-%s klines=%s",
        settings.mode,
        settings.tf_struct,
        settings.tf_entry,
        settings.top_pct * 100,
        settings.session_start,
        settings.session_end,
        settings.kline_limit,
    )
    bx = BingX(settings.bingx_api_key, settings.bingx_api_secret)
    state = State()
    oi_cache = OICache(settings.oi_state_path, settings.oi_lookback_min)
    await send_telegram(
        settings.telegram_token,
        settings.telegram_chat_id,
        (
            f"🚀 <b>Qulla Explosion arrancado</b>\n"
            f"MODE={settings.mode} · {settings.tf_struct}/{settings.tf_entry}\n"
            f"Sesión {settings.session_start}-{settings.session_end} UTC\n"
            f"Long={settings.allow_long} Short={settings.allow_short}\n"
            f"Scan cada {settings.scan_sec}s · max {settings.max_signals_day}/día"
        ),
    )
    n = 0
    hb_every = max(1, int(1800 / max(settings.scan_sec, 60)))
    try:
        while True:
            state._roll()
            try:
                info = await run_cycle(bx, state, oi_cache)
                log.info("ciclo %s", info)
                n += 1
                if n % hb_every == 0:
                    await send_telegram(
                        settings.telegram_token,
                        settings.telegram_chat_id,
                        format_heartbeat(info),
                    )
            except Exception:
                log.exception("ciclo error")
            await asyncio.sleep(settings.scan_sec)
    finally:
        await bx.close()


if __name__ == "__main__":
    asyncio.run(main())
