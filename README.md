# Qulla Explosion Bot (BingX + Telegram + Railway)

Escáner de **momentum tipo Qullamaggie** adaptado a perpetuos USDT de BingX.

## Setups

| Setup | Lado | Idea |
|-------|------|------|
| **BREAKOUT** | LONG | Leader (fuerte 7d/30d) + base estrecha sobre EMA10/20 + ruptura 15m + RVOL |
| **PARABOLIC** | SHORT | Extensión extrema sobre EMA20 + crack (vela débil) |
| **SQUEEZE** | L/S | Compresión Bollinger → expansión con volumen |

## Modos

- `MODE=SIGNAL` — solo Telegram (recomendado al inicio)
- `MODE=PAPER` — igual + estado local
- `MODE=LIVE` — requiere API keys; path de orden desactivado hasta configurar qty

## Deploy Railway

1. Sube este repo a GitHub (renombra `_gitignore` → `.gitignore`)
2. New Project → Deploy from repo
3. Volume en `/data` (recomendado: guarda estado diario y caché de OI entre deploys)
4. Pega variables de `railway_vars.txt` (completa `TELEGRAM_*`)
5. Start: `python bot.py` (definido en `railway.toml` y `Procfile`)

### Versión de Python

`runtime.txt` fija `python-3.12` y `RAILPACK_PYTHON_VERSION=3.12` lo refuerza.
No uses `3.11.9`: mise no encuentra attestations de GitHub para ese binario y el build falla.

## Local

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export TELEGRAM_TOKEN=... TELEGRAM_CHAT_ID=... MODE=SIGNAL
python bot.py
```

## Funding / OI

Funding en fracción (`0.0001` = 0.01% = 1 bps). Con `USE_FUNDING_AVG=true` se usa la media de ~21 pagos
(≈7d a 8h); el último pago solo bloquea si supera `FUNDING_EXTREME`.

- LONG bloqueado si funding > `FUNDING_LONG_MAX` (largos saturados) o último pago > `FUNDING_EXTREME`.
- LONG suma score si funding <= `-FUNDING_SHORT_BOOST` (cortos pagan).
- SHORT bloqueado si funding < `FUNDING_SHORT_MIN` (cortos saturados → riesgo de squeeze) o último pago < `-FUNDING_EXTREME`.
- SHORT suma score si funding >= `FUNDING_SHORT_BOOST`.
- **OI** (`USE_OI_FILTER`): Δ% del OI contra la muestra más cercana a `OI_LOOKBACK_MIN` (mín. 1h de edad).
  Solo ajusta score (LONG −8 si OI>+8% con funding+; SHORT +6 si OI>+5% con funding+, −5 si OI<−12%).
  Necesita ~1h de histórico tras arrancar; antes muestra `n/a`.
- El funding solo se consulta para los pares con setup; el OI de todos los leaders (histórico en `/data/oi_cache.json`).

## Notas

- Qullamaggie es swing en acciones; aquí se traduce a 4h estructura + 15m entrada.
- Win rate esperado bajo; el edge teórico está en asimetría (pocos winners grandes).
- No garantiza beneficios. Empieza en SIGNAL y mide 2–4 semanas.

Basado en reglas públicas del método de Kristjan Kullamägi (Qullamaggie): scan de líderes, base tras avance, stop estilo LOD/ATR, trail conceptual.
