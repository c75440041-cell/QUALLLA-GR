"""Telegram HTML."""
from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger("notify")


async def send_telegram(token: str, chat_id: str, text: str) -> None:
    if not token or not chat_id:
        log.warning("Telegram no configurado")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
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
        if r.status_code >= 400:
            log.error("Telegram %s %s", r.status_code, r.text[:200])


def format_signal(s: dict[str, Any]) -> str:
    side = s.get("side", "").upper()
    emoji = "🟢" if side == "LONG" else "🔴"
    return (
        f"{emoji} <b>QULLA EXPLOSION · {side}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Par:</b> {s.get('symbol')}\n"
        f"<b>Setup:</b> {s.get('setup')} · Score {s.get('score')}/100\n"
        f"<b>Mom 7d/30d:</b> {s.get('mom_s', 0):+.1f}% / {s.get('mom_l', 0):+.1f}%\n"
        f"\n"
        f"📍 Entrada ≈ <code>{s.get('entry')}</code>\n"
        f"🛑 SL <code>{s.get('sl')}</code>\n"
        f"🎯 TP <code>{s.get('tp')}</code> ({s.get('tp_r')}R)\n"
        f"\n"
        f"RVOL {s.get('rvol', 0):.2f}x · Base ATR {s.get('base_w', 0):.2f}\n"
        f"BTC régimen: {s.get('btc_regime', '—')}\n"
        f"TF {s.get('tf')} · MODE={s.get('mode')}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"<i>{s.get('note', '')}</i>"
    )


def format_heartbeat(info: dict[str, Any]) -> str:
    return (
        f"💓 <b>Qulla Explosion</b>\n"
        f"Pares {info.get('n_sym', 0)} · ciclo {info.get('cycle_s', 0):.0f}s\n"
        f"Leaders {info.get('n_leaders', 0)} · señales hoy {info.get('signals_today', 0)}\n"
        f"Régimen BTC: <b>{info.get('btc_regime', '—')}</b>\n"
        f"Embudo: {info.get('funnel', {})}\n"
        f"MODE={info.get('mode')} · sesión {info.get('session', '')}"
    )
