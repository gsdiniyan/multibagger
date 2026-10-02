"""
Keep this service awake during its working window when Railway's Serverless (app sleeping) mode is on.

Railway puts a serverless service to sleep 5-10 minutes after it last SENT any network traffic, and wakes it on the
next request that reaches it. Several services here go quiet for longer than that in the middle of the session
(diamond-directional runs every 15 minutes, diamond-stock-picks checks every 15), so on its own sleeping would stop
them between their own runs. This thread sends one small request every PING_SECONDS while inside the window
(AWAKE_WINDOW_IST, weekdays; default 08:50-16:45 IST, which covers the 09:10 pre-market check through the 16:10 /
16:05 after-close runs) and nothing outside it, so the service sleeps overnight and at weekends.

Something outside has to wake it in the morning: the trading dashboard requests each sleeping service at 08:50 IST
on weekdays (see its _wake_loop). Same file in every service that sleeps; never raises.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

IST = timezone(timedelta(hours=5, minutes=30))
PING_SECONDS = 180


def window(text: str | None = None) -> tuple[str, str]:
    """("08:50", "16:45") from "08:50-16:45"; the default on anything unparseable."""
    try:
        start, end = (text or os.environ.get("AWAKE_WINDOW_IST", "08:50-16:45")).split("-")
        datetime.strptime(start.strip(), "%H:%M"), datetime.strptime(end.strip(), "%H:%M")
        return start.strip(), end.strip()
    except (ValueError, AttributeError):
        return "08:50", "16:45"


def in_window(now: datetime, text: str | None = None) -> bool:
    start, end = window(text)
    return now.weekday() < 5 and start <= now.strftime("%H:%M") < end


def target() -> str:
    """Its own public /status when Railway gave it a domain (no third party involved), else a tiny 204 endpoint."""
    url = os.environ.get("KEEPALIVE_URL")
    if url:
        return url
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    return f"https://{domain}/status" if domain else "https://www.gstatic.com/generate_204"


def ping_once(now: datetime, get=requests.get) -> bool:
    """One keep-awake request if inside the window. Returns whether a request was sent."""
    if not in_window(now):
        return False
    try:
        get(target(), timeout=15)
    except Exception:
        pass                                   # a failed ping still sent packets, which is all that matters
    return True


def _loop() -> None:
    while True:
        ping_once(datetime.now(IST))
        time.sleep(PING_SECONDS)


def start() -> None:
    """Start the keep-awake thread (daemon). Off when KEEPALIVE=off."""
    if os.environ.get("KEEPALIVE", "on").lower() == "off":
        return
    threading.Thread(target=_loop, daemon=True, name="keepalive").start()
