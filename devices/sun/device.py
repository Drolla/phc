"""SunDevice: computes sunrise/sunset/dawn/dusk and solar elevation/azimuth
for a location, purely from astronomy (no network or hardware)."""

from datetime import datetime
from zoneinfo import ZoneInfo

from astral import Observer
from astral.sun import azimuth, elevation, sun

from core.device import Device
from core.registry import register_module


def _now(tzinfo):
    """Thin wrapper around datetime.now() so tests can monkeypatch a fixed
    reference time instead of depending on the wall clock."""
    return datetime.now(tzinfo)


@register_module("sun")
class SunDevice(Device):
    """Pure computation: today's sunrise/sunset/dawn/dusk/solar-noon plus the
    current solar elevation/azimuth, derived from latitude/longitude and the
    system clock via astral. No network access, no hardware -- receive() is
    plain synchronous math, cheap enough to run unmodified on the default
    thread-bridge path (no receive_async() override needed, unlike the
    network-bound meteoswiss/open_meteo modules).
    """

    def setup(self):
        """Build the astral Observer and timezone from this device's params."""
        self._observer = Observer(
            latitude=float(self.params["latitude"]),
            longitude=float(self.params["longitude"]),
            elevation=float(self.params.get("elevation") or 0.0),
        )
        # None is a valid astral tzinfo (falls back to system local time).
        tz_name = self.params.get("timezone")
        self._tzinfo = ZoneInfo(tz_name) if tz_name else None

    def receive(self) -> dict:
        """Compute the current sun position and today's sun events."""
        now = _now(self._tzinfo)
        sun_elevation = elevation(self._observer, now)
        state = {
            "elevation": sun_elevation,
            "azimuth": azimuth(self._observer, now),
            "is_daylight": sun_elevation > 0,
        }
        try:
            info = sun(self._observer, date=now.date(), tzinfo=self._tzinfo)
        except ValueError:
            # Polar day/night: the sun never crosses the dawn/dusk threshold
            # today, so there is no sunrise/sunset/dawn/dusk/noon to report.
            state.update(sunrise=None, sunset=None, solar_noon=None, dawn=None, dusk=None)
        else:
            state.update(
                sunrise=info["sunrise"].timestamp(),
                sunset=info["sunset"].timestamp(),
                solar_noon=info["noon"].timestamp(),
                dawn=info["dawn"].timestamp(),
                dusk=info["dusk"].timestamp(),
            )
        return state
