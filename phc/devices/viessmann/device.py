"""ViessmannDevice: one device of a Viessmann heating installation.

Read and written over the ViCare cloud API via the PyViCare library
(https://github.com/openviess/PyViCare). One PHC device maps to one device
of one gateway -- the heating system itself, the gateway, a room control.

PyViCare is synchronous, so this module implements the blocking
receive()/transmit() pair rather than the async one every other
network-backed module here uses, and PHC bridges it onto a worker thread.
The cost is that a call in flight is not cancellable the way a coroutine
would be; with a five-minute poll that is acceptable, and it buys not
reimplementing Viessmann's undocumented login flow -- which PyViCare
tracks, and Home Assistant's ViCare integration relies on.
"""

import logging
import os
import threading
import time

from PyViCare.PyViCare import PyViCare
from PyViCare.PyViCareUtils import (
    PyViCareInvalidConfigurationError,
    PyViCareInvalidCredentialsError,
    PyViCareNotPaidForError,
    PyViCareRateLimitError,
)

from phc.core.device import Device
from phc.core.intervals import parse_duration
from phc.core.registry import register_module

logger = logging.getLogger("phc.devices.viessmann")

_BACKOFF_MAX = 3600.0

# Retrying these cannot help until something changes, and every attempt
# spends an API call against a quota whose penalty is a 24-hour block.
_PERMANENT_ERRORS = (
    PyViCareInvalidCredentialsError,
    PyViCareInvalidConfigurationError,
    PyViCareNotPaidForError,
)


class ViessmannConfigError(Exception):
    """A device or endpoint points at something this account does not report."""


class ViessmannBackoffError(Exception):
    """An attempt was skipped because this account is in backoff."""


class _ViessmannState:
    """The PyViCare clients and feature caches one system's devices share.

    Per-system rather than per-process (see phc.core.device.Device.context):
    a client outliving its System would carry a stale token and device list
    into the next one.

    The locks here are threading.Locks, NOT the asyncio.Locks every other
    shared-state module in this repo uses -- PyViCare is synchronous, so
    this module's I/O runs on PHC's worker threads, where an asyncio.Lock
    would be both wrong and silently ineffective.
    """

    def __init__(self):
        # Keyed by (client_id, email), so devices of one account share one
        # login rather than each holding their own token.
        self.clients: dict[tuple[str, str], PyViCare] = {}
        self.client_lock = threading.Lock()
        # (installation_id, gateway_serial) -> (fetched_at, payload). Keyed
        # by gateway, not device: one call returns every device's features,
        # so devices on one gateway coalesce into a single fetch.
        self.features_cache: dict[tuple[int, str], tuple[float, dict]] = {}
        self.features_locks: dict[tuple[int, str], threading.Lock] = {}
        self.cache_lock = threading.Lock()
        # Per account, while it is in backoff.
        self.failed_at: dict[tuple[str, str], float] = {}
        self.backoff: dict[tuple[str, str], float] = {}
        self.last_error: dict[tuple[str, str], str] = {}

    def lock_for(self, key: tuple[int, str]) -> threading.Lock:
        """Return the fetch lock for one gateway, creating it if needed."""
        with self.cache_lock:
            lock = self.features_locks.get(key)
            if lock is None:
                lock = self.features_locks[key] = threading.Lock()
            return lock


@register_module("viessmann")
class ViessmannDevice(Device):
    """One device of a Viessmann installation, polled over the ViCare API.

    Each endpoint names a feature and one of its properties to read, and
    optionally a command to write. Both resolve against whatever the
    installation actually reports, so this module carries no per-product
    knowledge, and an endpoint naming a feature it does not have simply
    reads unavailable.
    """

    def setup(self):
        """Read this device's resolved params and join the shared state.

        Nothing logs in or fetches here: setup() runs while the config is
        still loading, and a wrong password must surface as this device
        being unhealthy on its first poll rather than as a failure to start
        PHC at all.
        """
        state = self.context.get("viessmann")
        if state is None:
            # get-then-assign, not setdefault, which would build a state
            # object per device just to discard all but the first.
            state = self.context["viessmann"] = _ViessmannState()
        self._state = state

        self._email = self.params["email"]
        self._password = self.params["password"]
        self._client_id = self.params["client_id"]
        self._account = (self._client_id, self._email)

        token_file = self.params.get("token_file")
        if token_file and not os.path.isabs(token_file):
            logger.warning(
                "%s: token_file %r is relative, so it resolves against the directory "
                "PHC was started from -- a service-started run will not find a token "
                "written by a hand-started one. Use an absolute path.",
                self.qualified_id, token_file)
        self._token_file = token_file

        self._installation_id = self.params.get("installation_id")
        self._gateway_serial = self.params.get("gateway_serial")
        # The .get() defaults mirror module.yaml, for a device built
        # directly rather than through load_system() (tests, scripts).
        self._device_id = str(self.params.get("device_id", "0"))
        self._cache_time = parse_duration(self.params.get("cache_time", "5m"))

        # Resolved once rather than per poll.
        self._reads = {
            key: (ep.params.get("feature"), ep.params.get("property") or "value")
            for key, ep in self.endpoints.items()
        }
        self._writes = {
            key: (ep.params.get("feature"), ep.params.get("command"),
                  ep.params.get("param"), ep.params.get("command_extras") or {})
            for key, ep in self.endpoints.items()
            if ep.writable and ep.params.get("command")
        }
        self._config = None

    # ----------------------------------------------------------------- reads

    def receive(self) -> dict:
        """Read this device's features -> {endpoint_key: raw_value}."""
        try:
            self._check_backoff()
            features = self._features()
        except Exception as exc:  # noqa: BLE001 -- classified by _report
            self._report(exc)
            features = None
        return {
            key: _extract(features, feature, prop)
            for key, (feature, prop) in self._reads.items()
        }

    def _features(self) -> dict:
        """Return {feature_name: entry} for THIS device, reusing it while fresh.

        The cache holds the gateway's whole payload and each device filters
        its own features out of it, so several devices of one gateway cost
        a single call between them.

        Double-checked locking: the fast path outside the lock keeps
        uncontended polls cheap, and the re-check inside it means that when
        several devices come due in the same tick, exactly one of them
        performs the fetch and the rest reuse its result.
        """
        config = self._device_config()
        key = (config.accessor.id, config.accessor.serial)

        mono = time.monotonic()
        cached = self._state.features_cache.get(key)
        if cached is not None and (mono - cached[0]) < self._cache_time:
            return _by_feature(cached[1], self._device_id)

        with self._state.lock_for(key):
            mono = time.monotonic()
            cached = self._state.features_cache.get(key)
            if cached is not None and (mono - cached[0]) < self._cache_time:
                return _by_feature(cached[1], self._device_id)
            payload = config.service.fetch_all_features(config.accessor)
            # Only a successful fetch reaches here, so a failure never
            # populates the cache and the next poll retries instead of
            # serving an error for a whole cache_time.
            self._state.features_cache[key] = (mono, payload)
            return _by_feature(payload, self._device_id)

    # ---------------------------------------------------------------- writes

    def transmit(self, state: dict) -> None:
        """Write endpoint values as the commands their features define."""
        try:
            self._check_backoff()
            config = self._device_config()
            features = self._features()
        except Exception as exc:  # noqa: BLE001 -- classified by _report
            self._report(exc)
            return

        wrote = False
        for key, value in state.items():
            feature, command, param, extras = self._writes.get(
                key, (None, None, None, {}))
            if command is None:
                self.report_failure(f"endpoint {key!r} has no command: to write with")
                continue
            try:
                data = _command_data(features, feature, command, param, extras, value)
                config.service.setProperty(config.accessor, feature, command, data)
            except Exception as exc:  # noqa: BLE001 -- classified by _report
                # One rejected write must not abandon the rest of the batch.
                self._report(exc, f"write to {feature}/{command} failed: ")
                continue
            wrote = True

        if wrote:
            # Re-read on the next poll: a value the installation clamped or
            # ignored would otherwise look applied until cache_time expired.
            self._state.features_cache.pop(
                (config.accessor.id, config.accessor.serial), None)

    # -------------------------------------------------------------- PyViCare

    def _device_config(self):
        """Return this device's PyViCareDeviceConfig, logging in if needed."""
        if self._config is not None:
            return self._config

        client = self._client()
        matches = [
            device for device in client.all_devices
            if str(device.device_id) == self._device_id
            and (self._installation_id is None
                 or str(device.accessor.id) == str(self._installation_id))
            and (self._gateway_serial is None
                 or str(device.accessor.serial) == str(self._gateway_serial))
        ]
        if not matches:
            available = ", ".join(
                f"{d.accessor.id}/{d.accessor.serial}/{d.device_id}"
                for d in client.all_devices) or "none"
            raise ViessmannConfigError(
                f"no device {self._device_id!r} in this account "
                f"(installation/gateway/device found: {available}); "
                f"run discover.py topology")
        if len(matches) > 1:
            raise ViessmannConfigError(
                f"device id {self._device_id!r} is ambiguous across "
                f"{len(matches)} gateways; set installation_id and gateway_serial "
                f"(run discover.py topology)")

        self._config = matches[0]
        return self._config

    def _client(self) -> PyViCare:
        """Return the shared PyViCare client for this account, logging in once.

        Held per account rather than per device because PyViCare renews its
        token by logging in again, so a client per device would multiply
        that.
        """
        state = self._state
        with state.client_lock:
            client = state.clients.get(self._account)
            if client is not None:
                return client
            client = PyViCare()
            # One bulk call per gateway serves every device on it, instead
            # of one call per device per poll.
            client.loadViaGateway(True)
            # 0 disables PyViCare's own cache: stacking it on this module's
            # would mean two lifetimes to reason about against one quota.
            client.setCacheDuration(0)
            client.initWithCredentials(
                self._email, self._password, self._client_id, self._token_file)
            state.clients[self._account] = client
            state.failed_at.pop(self._account, None)
            state.backoff.pop(self._account, None)
            state.last_error.pop(self._account, None)
            return client

    def _check_backoff(self) -> None:
        """Skip this attempt if the account is still in backoff.

        Checked before reading and before writing, not just before logging
        in: the login happens once and is then cached, so a check only
        there would let a rate-limited account keep spending a call on
        every poll -- which is how a temporary block becomes a 24-hour one.
        """
        state = self._state
        failed_at = state.failed_at.get(self._account)
        if failed_at is None:
            return
        if (time.monotonic() - failed_at) < state.backoff.get(self._account, 0.0):
            raise ViessmannBackoffError(state.last_error[self._account])

    def _report(self, exc: Exception, prefix: str = "") -> None:
        """Report one failure, arming backoff for the kinds retrying cannot fix."""
        if isinstance(exc, ViessmannBackoffError):
            self.report_failure(f"{prefix}{exc}")
            return

        if isinstance(exc, PyViCareRateLimitError):
            # The quota recovers on its own, but every further call while
            # blocked counts against it, so wait rather than keep polling.
            message = f"API rate limit exceeded, resets at {exc.limitResetDate}"
            self._arm_backoff(message)
        elif isinstance(exc, _PERMANENT_ERRORS):
            message = (
                f"{type(exc).__name__}: {exc}. Check email, password and client_id, "
                f"then verify them with "
                f"`python -m phc.devices.viessmann.discover login`")
            self._arm_backoff(message)
        else:
            # Transient, or a config mistake: the next poll tries again.
            message = f"{type(exc).__name__}: {exc}"
        self.report_failure(f"{prefix}{message}")

    def _arm_backoff(self, message: str) -> None:
        """Hold off further attempts for this account, doubling the wait."""
        state = self._state
        previous = state.backoff.get(self._account, 0.0)
        state.backoff[self._account] = min(
            max(previous * 2, self._cache_time or 60.0), _BACKOFF_MAX)
        state.failed_at[self._account] = time.monotonic()
        state.last_error[self._account] = message
        # Drop the client so the next attempt after the wait logs in again.
        state.clients.pop(self._account, None)


def _by_feature(payload, device_id=None) -> dict:
    """Index one device's features from a response, by feature name.

    PyViCare's via-gateway fetch returns EVERY device of the gateway in one
    payload -- that is what makes sibling devices cheap, but it means the
    entries must be filtered per device, or a gateway would appear to
    report the heat pump's features. Each entry names its device in its own
    `uri`, as PyViCare's own filter_features_for_device matches on.

    device_id None returns the whole payload.
    """
    entries = (payload or {}).get("data") or []
    if device_id is not None:
        segment = f"/devices/{device_id}/"
        entries = [e for e in entries if segment in e.get("uri", "")]
    return {entry["feature"]: entry for entry in entries if "feature" in entry}


def _extract(features: dict | None, feature_name, property_name):
    """Return one property's value out of a features response.

    None whenever the value is unavailable: a failed fetch, a feature this
    installation does not report, a feature reported but switched off, or a
    property that is not one of that feature's -- so a mistyped feature or
    property reads blank rather than silently wrong.

    isEnabled is honoured because a disabled feature may still carry a
    value that means nothing. isReady is deliberately not: it describes the
    device's commissioning state, not whether this reading is good.
    """
    if features is None or not feature_name:
        return None
    feature = features.get(feature_name)
    if feature is None or not feature.get("isEnabled", True):
        return None
    prop = (feature.get("properties") or {}).get(property_name)
    if not isinstance(prop, dict):
        return None
    return prop.get("value")


def _command_data(features, feature_name, command, param, extras, value) -> dict:
    """Build one command's request body, resolving its parameter name.

    The command's declared parameters come from the installation's own
    response rather than from a table here, so a command this module has
    never seen still works: an endpoint naming a single-parameter command
    needs no `param:`, while one naming a command that sets several values
    at once must say which of them it writes.
    """
    feature = (features or {}).get(feature_name)
    if feature is None:
        raise ViessmannConfigError(
            f"feature {feature_name!r} is not reported by this installation")

    spec = (feature.get("commands") or {}).get(command)
    if spec is None:
        available = ", ".join(sorted(feature.get("commands") or {})) or "none"
        raise ViessmannConfigError(
            f"feature {feature_name!r} has no command {command!r} "
            f"(available: {available})")
    if not spec.get("isExecutable", True):
        raise ViessmannConfigError(
            f"{feature_name}/{command} is not executable right now")

    declared = list(spec.get("params") or {})
    if param is None:
        if len(declared) != 1:
            raise ViessmannConfigError(
                f"{feature_name}/{command} takes {len(declared)} parameters "
                f"({', '.join(declared) or 'none'}); set param: on the endpoint "
                f"to say which one it writes")
        param = declared[0]

    data = dict(extras)
    data[param] = value
    return data
