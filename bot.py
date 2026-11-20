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
        return json.loads(p.read_text())
    except Exception:
        return {}


def _save(path: str, data: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(p)


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


def _ticker_vol(t: dict) -> float:
    """BingX usa claves distintas según endpoint/versión."""
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
        log.warning("ticker list vacía — revisa BingX API")
        return []
    # debug una sola vez por proceso
    if not getattr(universe, "_logged_sample", False):
        sample = tickers[0] if isinstance(tickers[0], dict) else {}
        log.info("ticker sample keys=%s symbol=%s vol=%s", list(sample.keys())[:20], _ticker_symbol(sample), _ticker_vol(sample))
        universe._logged_sample = True  # type: ignore

    q = settings.quote
    rows: list[tuple[str, float]] = []
    skipped_quote = 0
    skipped_vol = 0
    for t in tickers:
        if not isinstance(t, dict):
            continue
        sym = _ticker_symbol(t)
        if not sym:
            continue
        # aceptar BTC-USDT o BTCUSDT
        if sym.endswith(q) and not sym.endswith(f"-{q}"):
            base = sym[: -len(q)]
            sym = f"{base}-{q}"
        if not sym.endswith(f"-{q}"):
            skipped_quote += 1
            continue
        vol = _ticker_vol(t)
        if vol < settings.min_quote_vol_24h:
            skipped_vol += 1
            # si vol siempre 0 por clave mal parseada, no descartar aún
            if vol > 0:
                continue
        rows.append((sym, vol))

    # si el filtro de volumen dejó todo fuera pero había pares USDT, relajar
    if not rows:
        log.warning(
            "universo vacío tras filtros (skip_quote=%s skip_vol=%s n_raw=%s) — relajando volumen",
            skipped_quote,
            skipped_vol,
            len(tickers),
        )
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

    rows.sort(key=lambda x: x[1], reverse=True)
    out = [s for s, _ in rows[: settings.max_symbols]]
    log.info("universo n=%s (raw=%s skip_quote=%s)", len(out), len(tickers), skipped_quote)
    return out


async def scan_one(
    bx: BingX,
    symbol: str,
    btc_reg: str,
    sem: asyncio.Semaphore,
) -> Signal | None:
    async with sem:
        try:
            kl_s = await bx.klines(symbol, settings.tf_struct, settings.kline_limit)
            kl_e = await bx.klines(symbol, settings.tf_entry, min(120, settings.kline_limit))
        except Exception as e:
            log.debug("%s klines: %s", symbol, e)
            return None

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

    if not candidates:
        return None
    candidates.sort(key=lambda x: x.score, reverse=True)
    best = candidates[0]
    best.btc_regime = btc_reg
    best.tf = settings.tf_entry
    return best


async def run_cycle(bx: BingX, state: State) -> dict[str, Any]:
    t0 = time.time()
    if not _in_session():
        return {"n_sym": 0, "cycle_s": 0, "skipped": "session", "hour": _utc_hour()}

    if state.data.get("signals_today", 0) >= settings.max_signals_day:
        return {"n_sym": 0, "cycle_s": 0, "skipped": "max_signals"}

    symbols = await universe(bx)
    try:
        btc_kl = await bx.klines(f"BTC-{settings.quote}", settings.tf_struct, 100)
        reg = btc_regime(btc_kl)
    except Exception:
        reg = "unknown"

    # pre-rank leaders by momentum (cheap pass with struct TF)
    sem = asyncio.Semaphore(settings.workers)
    mom_list: list[tuple[str, float, float]] = []

    async def _mom(sym: str) -> None:
        async with sem:
            try:
                kl = await bx.klines(sym, settings.tf_struct, settings.kline_limit)
            except Exception:
                return
        ms, ml = momentum_rank(kl, settings.mom_bars_short, settings.mom_bars_long)
        if ms >= settings.min_mom_pct * 0.5 or abs(ms) >= settings.min_mom_pct * 0.6:
            mom_list.append((sym, ms, ml))

    await asyncio.gather(*[_mom(s) for s in symbols])
    # top by abs short momentum then long
    mom_list.sort(key=lambda x: (abs(x[1]), abs(x[2])), reverse=True)
    n_keep = max(20, int(len(symbols) * settings.top_pct))
    leaders = [x[0] for x in mom_list[:n_keep]]
    # always include strong absolute movers even if negative (for shorts)
    for sym, ms, ml in mom_list:
        if abs(ms) >= settings.min_mom_pct and sym not in leaders:
            leaders.append(sym)
        if len(leaders) >= n_keep + 15:
            break

    funnel = {"leaders": len(leaders), "setups": 0, "sent": 0}
    signals: list[Signal] = []
    results = await asyncio.gather(
        *[scan_one(bx, s, reg, sem) for s in leaders],
        return_exceptions=True,
    )
    for r in results:
        if isinstance(r, Signal):
            signals.append(r)
            funnel["setups"] += 1
    signals.sort(key=lambda x: x.score, reverse=True)

    for sig in signals:
        if state.already(sig.symbol, sig.side):
            continue
        if state.data.get("signals_today", 0) >= settings.max_signals_day:
            break
        if sig.score < 55:
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
        }
        await send_telegram(
            settings.telegram_token,
            settings.telegram_chat_id,
            format_signal(payload),
        )
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
        "Qulla Explosion MODE=%s TF=%s/%s TOP=%.0f%% session=%s-%s",
        settings.mode,
        settings.tf_struct,
        settings.tf_entry,
        settings.top_pct * 100,
        settings.session_start,
        settings.session_end,
    )
    bx = BingX(settings.bingx_api_key, settings.bingx_api_secret)
    state = State()
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
                info = await run_cycle(bx, state)
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
