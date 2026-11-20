"""Qulla Explosion Bot — config desde env (Railway)."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _b(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # SIGNAL = solo Telegram | PAPER = simula | LIVE = órdenes BingX
    mode: str = "SIGNAL"
    quote: str = "USDT"
    max_symbols: int = 180
    scan_sec: int = 180
    workers: int = 14
    tf_struct: str = "4h"   # estructura tipo daily
    tf_entry: str = "15m"   # trigger tipo opening range
    # Debe ser >= mom_bars_long + margen, si no el momentum 30d no se calcula.
    kline_limit: int = 220

    # Momentum scan (estilo top 1-2%)
    mom_bars_short: int = 42   # ~7d en 4h
    mom_bars_long: int = 180   # ~30d en 4h
    top_pct: float = 0.08      # top 8% por momentum (mínimo 30 leaders)
    min_mom_pct: float = 18.0  # reservado (no se usa como filtro duro)

    # Breakout Qulla-like
    prior_min_pct: float = 25.0
    base_bars: int = 12
    base_max_width_atr: float = 2.8
    break_buf_atr: float = 0.15
    rvol_min: float = 1.4
    rvol_len: int = 20
    ema_fast: int = 10
    ema_slow: int = 20

    # Parabolic short
    para_up_days: int = 5
    para_ext_atr: float = 3.5
    para_min_mom: float = 40.0

    # Compression → expansion (idea extra)
    use_squeeze: bool = True
    bb_len: int = 20
    squeeze_pct: float = 25.0  # percentil ancho BB bajo = comprimido

    # Funding (perps). Valores como fracción: 0.0001 = 0.01% = 1 bps
    use_funding_filter: bool = True
    # LONG bloqueado si funding > este valor (largos saturados)
    funding_long_max: float = 0.0005    # 0.05%
    # SHORT bloqueado si funding < este valor (cortos saturados → riesgo de squeeze)
    funding_short_min: float = -0.0001  # permite ligeramente negativo
    # Funding >= esto suma score al SHORT; <= -esto suma score al LONG
    funding_short_boost: float = 0.0003
    # Crowding extremo: bloquea el lado saturado aunque el promedio sea normal
    funding_extreme: float = 0.001      # 0.10%
    use_funding_avg: bool = True        # usa media de ~21 pagos (≈7d a 8h)

    # Open interest: ajusta score según Δ% de OI (no bloquea)
    use_oi_filter: bool = True
    oi_lookback_min: int = 240          # ventana para Δ% (4h, igual que la estructura)
    oi_state_path: str = "/data/oi_cache.json"

    # Filtros
    require_btc_align: bool = True
    allow_long: bool = True
    allow_short: bool = True
    min_quote_vol_24h: float = 50_000
    session_start: int = 0
    session_end: int = 24
    max_signals_day: int = 8
    cooldown_bars: int = 1  # reservado: hoy se dedupe par+lado por día

    # Riesgo
    risk_pct: float = 0.5
    max_lev: float = 5.0
    sl_atr: float = 1.2
    tp_r: float = 2.5
    max_loss_streak: int = 3
    max_day_loss_pct: float = 2.5

    telegram_token: str = ""
    telegram_chat_id: str = ""
    bingx_api_key: str = ""
    bingx_api_secret: str = ""
    state_path: str = "/data/state.json"

    @classmethod
    def from_env(cls) -> "Settings":
        mode = os.getenv("MODE", "SIGNAL").strip().upper()
        if mode not in {"SIGNAL", "PAPER", "LIVE"}:
            mode = "SIGNAL"

        mom_bars_short = _i("MOM_BARS_SHORT", 42)
        mom_bars_long = _i("MOM_BARS_LONG", 180)
        # Aunque en Railway quede KLINE_LIMIT=120, forzamos velas suficientes
        # para el momentum largo (antes daba 0.0 siempre).
        kline_limit = max(_i("KLINE_LIMIT", 220), mom_bars_long + 20, mom_bars_short + 20)

        return cls(
            mode=mode,
            quote=os.getenv("QUOTE", "USDT").strip().upper(),
            max_symbols=max(20, _i("MAX_SYMBOLS", 180)),
            scan_sec=max(60, _i("SCAN_SEC", 180)),
            workers=max(2, min(32, _i("WORKERS", 14))),
            tf_struct=os.getenv("TF_STRUCT", "4h"),
            tf_entry=os.getenv("TF_ENTRY", "15m"),
            kline_limit=kline_limit,
            mom_bars_short=mom_bars_short,
            mom_bars_long=mom_bars_long,
            top_pct=_f("TOP_PCT", 0.08),
            min_mom_pct=_f("MIN_MOM_PCT", 18.0),
            prior_min_pct=_f("PRIOR_MIN_PCT", 25.0),
            base_bars=_i("BASE_BARS", 12),
            base_max_width_atr=_f("BASE_MAX_WIDTH_ATR", 2.8),
            break_buf_atr=_f("BREAK_BUF_ATR", 0.15),
            rvol_min=_f("RVOL_MIN", 1.4),
            rvol_len=_i("RVOL_LEN", 20),
            ema_fast=_i("EMA_FAST", 10),
            ema_slow=_i("EMA_SLOW", 20),
            para_up_days=_i("PARA_UP_DAYS", 5),
            para_ext_atr=_f("PARA_EXT_ATR", 3.5),
            para_min_mom=_f("PARA_MIN_MOM", 40.0),
            use_squeeze=_b("USE_SQUEEZE", "true"),
            bb_len=_i("BB_LEN", 20),
            squeeze_pct=_f("SQUEEZE_PCT", 25.0),
            use_funding_filter=_b("USE_FUNDING_FILTER", "true"),
            funding_long_max=_f("FUNDING_LONG_MAX", 0.0005),
            funding_short_min=_f("FUNDING_SHORT_MIN", -0.0001),
            funding_short_boost=_f("FUNDING_SHORT_BOOST", 0.0003),
            funding_extreme=_f("FUNDING_EXTREME", 0.001),
            use_funding_avg=_b("USE_FUNDING_AVG", "true"),
            use_oi_filter=_b("USE_OI_FILTER", "true"),
            oi_lookback_min=max(60, _i("OI_LOOKBACK_MIN", 240)),
            oi_state_path=os.getenv("OI_STATE_PATH", "/data/oi_cache.json"),
            require_btc_align=_b("REQUIRE_BTC_ALIGN", "true"),
            allow_long=_b("ALLOW_LONG", "true"),
            allow_short=_b("ALLOW_SHORT", "true"),
            min_quote_vol_24h=_f("MIN_QUOTE_VOL_24H", 50000),
            session_start=_i("SESSION_START", 0),
            session_end=_i("SESSION_END", 24),
            max_signals_day=_i("MAX_SIGNALS_DAY", 8),
            risk_pct=_f("RISK_PCT", 0.5),
            max_lev=_f("MAX_LEV", 5.0),
            sl_atr=_f("SL_ATR", 1.2),
            tp_r=_f("TP_R", 2.5),
            max_loss_streak=_i("MAX_LOSS_STREAK", 3),
            max_day_loss_pct=_f("MAX_DAY_LOSS_PCT", 2.5),
            telegram_token=os.getenv("TELEGRAM_TOKEN", "").strip(),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            bingx_api_key=os.getenv("BINGX_API_KEY", "").strip(),
            bingx_api_secret=os.getenv("BINGX_API_SECRET", "").strip(),
            state_path=os.getenv("STATE_PATH", "/data/state.json"),
        )


settings = Settings.from_env()
