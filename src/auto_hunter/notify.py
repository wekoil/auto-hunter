"""Alert delivery. Telegram if configured, otherwise just the console."""

from __future__ import annotations

import logging
import os

import httpx

from auto_hunter.pipeline import Alert

log = logging.getLogger(__name__)

TELEGRAM_MAX_LEN = 4000


def format_alert(alert: Alert) -> str:
    l = alert.listing
    price = f"{l.price_czk:,} Kč".replace(",", " ") if l.price_czk else "price on request"
    if alert.kind == "price_drop" and alert.old_price:
        drop = (alert.old_price - (l.price_czk or 0)) / alert.old_price * 100
        head = f"📉 Price drop −{drop:.0f}%: {alert.old_price:,} → {price}".replace(",", " ")
    else:
        head = f"🆕 {price}"
    specs = ", ".join(
        s for s in [
            str(l.year) if l.year else None,
            f"{l.mileage_km:,} km".replace(",", " ") if l.mileage_km is not None else None,
            f"{l.power_kw} kW" if l.power_kw else None,
            l.fuel, l.gearbox, l.district, l.seller_type,
        ] if s
    )
    return f"{head}\n{l.title}\n{specs}\n[{', '.join(alert.profiles)}] {l.url}"


def send_alerts(alerts: list[Alert]) -> None:
    if not alerts:
        return
    messages = [format_alert(a) for a in alerts]
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        print("\n\n".join(messages))
        return
    for chunk in _chunks(messages):
        try:
            resp = httpx.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True},
                timeout=20,
            )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            # Don't log the URL: it contains the bot token.
            log.error("Telegram send failed: %s", type(e).__name__)


def _chunks(messages: list[str]) -> list[str]:
    chunks, current = [], ""
    for m in messages:
        if current and len(current) + len(m) + 2 > TELEGRAM_MAX_LEN:
            chunks.append(current)
            current = ""
        current = f"{current}\n\n{m}" if current else m
    if current:
        chunks.append(current)
    return chunks
