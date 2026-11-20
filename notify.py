"""Telegram HTML."""
from __future__ import annotations

import html
import logging
from typing import Any

import httpx

log = logging.getLogger("notify")


async def send_telegram(token: str, chat_id: str, text: str) -> bool:
    """Envía el mensaje. True si Telegram lo aceptó; False si falta config o falló (no lanza)."""
    if not token or not chat_id:
        log.warning("Telegram no configurado")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
    except httpx.HTTPError as e:
        log.error("Telegram red: %s", e)
        return False
    if r.status_code >= 400:
        log.error("Telegram %s %s", r.status_code, r.text[:200])
        return False
    return True


def _fmt(v: Any, suffix: str = "") -> str:
    return "n/a" if v is None else f"{v}{suffix}"


def format_signal(s: dict[str, Any]) -> str:
    side = str(s.get("side", "")).upper()
    emoji = "🟢" if side == "LONG" else "🔴"
    esc = lambda x: html.escape(str(x))  # noqa: E731
    return (
        f"{emoji} <b>QULLA EXPLOSION · {esc(side)}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Par:</b> {esc(s.get('symbol'))}\n"
        f"<b>Setup:</b> {esc(s.get('setup'))} · Score {s.get('score')}/100\n"
        f"<b>Mom 7d/30d:</b> {s.get('mom_s', 0):+.1f}% / {s.get('mom_l', 0):+.1f}%\n"
        f"<b>Funding:</b> {_fmt(s.get('funding'))} bps (avg {_fmt(s.get('funding_avg'))})\n"
        f"<b>OI Δ:</b> {_fmt(s.get('oi_chg_pct'), '%')}\n"
        f"\n"
        f"📍 Entrada ≈ <code>{s.get('entry')}</code>\n"
        f"🛑 SL <code>{s.get('sl')}</code>\n"
        f"🎯 TP <code>{s.get('tp')}</code> ({s.get('tp_r')}R)\n"
        f"\n"
        f"RVOL {s.get('rvol', 0):.2f}x · Base ATR {s.get('base_w', 0):.2f}\n"
        f"BTC régimen: {esc(s.get('btc_regime', '—'))}\n"
        f"TF {esc(s.get('tf'))} · MODE={esc(s.get('mode'))}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"<i>{esc(s.get('note', ''))}</i>"
    )


def format_heartbeat(info: dict[str, Any]) -> str:
    return (
        f"💓 <b>Qulla Explosion</b>\n"
        f"Pares {info.get('n_sym', 0)} · ciclo {info.get('cycle_s', 0):.0f}s\n"
        f"Leaders {info.get('n_leaders', 0)} · señales hoy {info.get('signals_today', 0)}\n"
        f"Régimen BTC: <b>{html.escape(str(info.get('btc_regime', '—')))}</b>\n"
        f"Embudo: {html.escape(str(info.get('funnel', {})))}\n"
        f"MODE={html.escape(str(info.get('mode')))} · sesión {html.escape(str(info.get('session', '')))}"
    )
