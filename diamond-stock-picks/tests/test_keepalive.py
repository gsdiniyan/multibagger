"""keepalive.py: pings only inside the weekday window, so the service can sleep the rest of the time."""
from datetime import datetime, timedelta, timezone

import keepalive as ka

IST = timezone(timedelta(hours=5, minutes=30))


def at(day, hhmm):
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 10, day, h, m, tzinfo=IST)     # 2026-10-05 is a Monday


def test_window_bounds():
    assert not ka.in_window(at(5, "08:49")) and ka.in_window(at(5, "08:50"))
    assert ka.in_window(at(5, "16:44")) and not ka.in_window(at(5, "16:45"))
    assert not ka.in_window(at(3, "10:00")) and not ka.in_window(at(4, "10:00"))     # Saturday, Sunday
    assert ka.in_window(at(5, "07:00"), "06:30-08:00")
    assert ka.window("junk") == ("08:50", "16:45")


def test_ping_only_inside_window(monkeypatch):
    calls = []
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "svc.up.railway.app")
    monkeypatch.delenv("KEEPALIVE_URL", raising=False)
    monkeypatch.delenv("AWAKE_WINDOW_IST", raising=False)
    get = lambda url, timeout: calls.append(url)
    assert ka.ping_once(at(5, "12:00"), get) is True and calls == ["https://svc.up.railway.app/status"]
    assert ka.ping_once(at(5, "20:00"), get) is False and len(calls) == 1


def test_a_failed_ping_never_raises(monkeypatch):
    def boom(url, timeout):
        raise OSError("network down")
    assert ka.ping_once(at(5, "12:00"), boom) is True


def test_target_fallbacks(monkeypatch):
    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)
    monkeypatch.delenv("KEEPALIVE_URL", raising=False)
    assert ka.target() == "https://www.gstatic.com/generate_204"
    monkeypatch.setenv("KEEPALIVE_URL", "https://example.test/x")
    assert ka.target() == "https://example.test/x"
