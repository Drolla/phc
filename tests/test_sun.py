"""SunDevice is pure computation (astral against latitude/longitude + the
system clock), so these tests monkeypatch modules.sun.device._now to a fixed
reference time instead of depending on the wall clock -- deterministic
without needing to mock astral itself.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from core.endpoint import Endpoint
from core.scheduler import Scheduler
from modules.sun import device as sun_device
from modules.sun.device import SunDevice

_ZURICH = ZoneInfo("Europe/Zurich")
_ENDPOINT_KEYS = ["sunrise", "sunset", "solar_noon", "dawn", "dusk", "elevation", "azimuth", "is_daylight"]


def _device(latitude=47.3769, longitude=8.5417, timezone="Europe/Zurich"):
    params = {"latitude": latitude, "longitude": longitude}
    if timezone is not None:
        params["timezone"] = timezone
    return SunDevice(
        "sun",
        params=params,
        endpoints=[Endpoint(key) for key in _ENDPOINT_KEYS],
        update_interval=0.0,
    )


def _tick(dev, monkeypatch, fixed_now):
    monkeypatch.setattr(sun_device, "_now", lambda tzinfo: fixed_now)
    scheduler = Scheduler({"sun": dev})
    scheduler.tick(now=0.0)
    scheduler.close()


def test_sun_reports_daytime_state_at_local_noon(monkeypatch):
    dev = _device()
    _tick(dev, monkeypatch, datetime(2026, 7, 23, 13, 0, 0, tzinfo=_ZURICH))

    assert dev.get("is_daylight") is True
    assert dev.get("elevation") > 0
    assert 0.0 <= dev.get("azimuth") < 360.0
    assert dev.get("sunrise") < dev.get("solar_noon") < dev.get("sunset")
    assert dev.get("dawn") <= dev.get("sunrise")
    assert dev.get("dusk") >= dev.get("sunset")


def test_sun_reports_nighttime_state_at_local_midnight(monkeypatch):
    dev = _device()
    _tick(dev, monkeypatch, datetime(2026, 1, 23, 0, 0, 0, tzinfo=_ZURICH))

    assert dev.get("is_daylight") is False
    assert dev.get("elevation") < 0
    # Times are still reported (Zurich has a sunrise/sunset every day).
    assert dev.get("sunrise") is not None
    assert dev.get("sunset") is not None


def test_sun_reports_none_times_during_polar_day(monkeypatch):
    # Svalbard, midsummer: the sun never dips 6 degrees below the horizon,
    # so astral.sun.sun() raises ValueError -- exercised end to end.
    dev = _device(latitude=78.2232, longitude=15.6267, timezone="UTC")
    _tick(dev, monkeypatch, datetime(2026, 6, 21, 12, 0, 0, tzinfo=ZoneInfo("UTC")))

    assert dev.get("sunrise") is None
    assert dev.get("sunset") is None
    assert dev.get("solar_noon") is None
    assert dev.get("dawn") is None
    assert dev.get("dusk") is None
    # Elevation/azimuth are always computable, polar day or not.
    assert dev.get("elevation") > 0
    assert dev.get("is_daylight") is True


def test_sun_defaults_to_local_timezone_when_unset():
    dev = _device(timezone=None)
    assert dev._tzinfo is None
