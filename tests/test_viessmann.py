"""Viessmann device: reads features and writes commands over the ViCare API.

The module delegates authentication and transport to PyViCare, so these
tests stand a fake ViCareService in its place -- an object with the same
fetch_all_features()/setProperty() surface -- rather than a local HTTP
server as tests/test_meteoswiss.py and tests/test_solaredge.py do for
modules that own their own transport. What is faked here is a third-party
boundary, not any of PHC's own code: extraction, caching, backoff and the
read/write mapping all still run end to end through a real Scheduler.

The feature payloads below are trimmed copies of what a real Vitocal 250-A
installation returns, including its quirks -- properties named `status`,
`active`, `phase`, `hours`, `slope`/`shift`; a command that takes two
parameters; and commands reported as not executable.

No test here may reach api.viessmann-climatesolutions.com or read
credentials from the environment: the free API tier allows 120 calls per
10 minutes, and exceeding it blocks the account for 24 hours.
"""

import pytest

from phc.core.config import load_system
from phc.core.endpoint import Endpoint
from phc.core.scheduler import Scheduler
from phc.devices.viessmann.device import ViessmannDevice

INSTALLATION = 2800335
GATEWAY = "7637415046982246"


def _feature(name, properties, commands=None, enabled=True):
    """One entry of a features response, shaped as the API returns it."""
    return {
        "feature": name,
        "isEnabled": enabled,
        "isReady": True,
        "properties": properties,
        "commands": commands or {},
        "uri": (f"https://api.viessmann-climatesolutions.com/iot/v2/features"
                f"/installations/{INSTALLATION}/gateways/{GATEWAY}"
                f"/devices/0/features/{name}"),
    }


def _num(value, unit=""):
    return {"type": "number", "value": value, "unit": unit}


FEATURES = {"data": [
    _feature("heating.sensors.temperature.outside",
             {"value": _num(14.4, "celsius"),
              "status": {"type": "string", "value": "connected"}}),
    _feature("heating.dhw.sensors.temperature.dhwCylinder",
             {"value": _num(44.2, "celsius")}),
    _feature("heating.dhw.temperature.main",
             {"value": _num(52, "celsius")},
             {"setTargetTemperature": {
                 "name": "setTargetTemperature", "isExecutable": True,
                 "params": {"temperature": {
                     "type": "number", "required": True,
                     "constraints": {"min": 10, "max": 60, "stepping": 1}}}}}),
    _feature("heating.compressors.0",
             {"active": {"type": "boolean", "value": True},
              "phase": {"type": "string", "value": "heating"}},
             # Reported, but the API refuses to execute it.
             {"setActive": {
                 "name": "setActive", "isExecutable": False,
                 "params": {"active": {"type": "boolean", "required": True,
                                       "constraints": {}}}}}),
    _feature("heating.compressors.0.statistics",
             {"starts": _num(3078), "hours": _num(5903.7, "hour")}),
    _feature("heating.cop.total", {"value": _num(3)}),
    _feature("heating.circuits.2.circulation.pump",
             {"status": {"type": "string", "value": "on"}}),
    _feature("heating.circuits.2.heating.curve",
             {"slope": _num(1.1), "shift": _num(0)},
             {"setCurve": {
                 "name": "setCurve", "isExecutable": True,
                 "params": {
                     "slope": {"type": "number", "required": True,
                               "constraints": {"min": 0, "max": 3.5}},
                     "shift": {"type": "number", "required": True,
                               "constraints": {"min": -15, "max": 40}}}}}),
    _feature("heating.circuits.2.operating.modes.active",
             {"value": {"type": "string", "value": "dhwAndHeating"}},
             {"setMode": {
                 "name": "setMode", "isExecutable": True,
                 "params": {"mode": {
                     "type": "string", "required": True,
                     "constraints": {"enum": ["dhw", "dhwAndHeating",
                                              "standby"]}}}}}),
    # Switched off: its value is stale and must read as unavailable.
    _feature("heating.solar.power.production", {"value": _num(999)},
             enabled=False),
]}


class _FakeAccessor:
    def __init__(self, device_id, installation=INSTALLATION, serial=GATEWAY):
        self.id = installation
        self.serial = serial
        self.device_id = device_id


class _FakeService:
    """Stands in for PyViCare's ViCareService.

    Counts fetches so a test can prove caching avoided a call, records
    writes so it can assert the exact body sent, and can be told to raise
    instead -- which is how the failure paths are driven.
    """

    def __init__(self, payload=FEATURES, error=None):
        self.payload = payload
        self.error = error
        self.fetches = 0
        self.writes = []
        self.write_error = None

    def fetch_all_features(self, accessor):
        self.fetches += 1
        if self.error is not None:
            raise self.error
        return self.payload

    def setProperty(self, accessor, feature, command, data):  # noqa: N802
        if self.write_error is not None:
            raise self.write_error
        self.writes.append((accessor.device_id, feature, command, data))
        return {"data": {"success": True}}


class _FakeDeviceConfig:
    def __init__(self, device_id, service, installation=INSTALLATION,
                 serial=GATEWAY):
        self.accessor = _FakeAccessor(device_id, installation, serial)
        self.service = service
        self.device_id = device_id


def _device(endpoints, *, service=None, context=None, device_id="0",
            cache_time="5m", params=None, installation=INSTALLATION,
            serial=GATEWAY, device_key=None):
    """Build a ViessmannDevice with PyViCare's client already stood in for.

    Assigning _config is what keeps the login out of these tests: it is
    the only thing _features()/transmit() need from PyViCare, and building
    it here is equivalent to a successful initWithCredentials().
    """
    service = service if service is not None else _FakeService()
    merged = {
        "email": "someone@example.com",
        "password": "secret",
        "client_id": "0123456789abcdef",
        "device_id": device_id,
        "cache_time": cache_time,
    }
    merged.update(params or {})
    device = ViessmannDevice(
        device_key or f"heatpump-{device_id}",
        params=merged,
        endpoints=endpoints,
        update_interval=0.0,
        context=context,
    )
    device._config = _FakeDeviceConfig(device_id, service, installation, serial)
    return device


def _tick(*devices, now=0.0):
    scheduler = Scheduler({d.qualified_id: d for d in devices})
    try:
        scheduler.tick(now=now)
    finally:
        scheduler.close()


def test_reads_every_property_shape():
    """Values come back from the differently-named properties the API uses."""
    service = _FakeService()
    device = _device([
        Endpoint("outside", value_type="float",
                 params={"feature": "heating.sensors.temperature.outside",
                         "property": "value"}),
        Endpoint("sensor_status", value_type="str",
                 params={"feature": "heating.sensors.temperature.outside",
                         "property": "status"}),
        Endpoint("compressor_active", value_type="bool",
                 params={"feature": "heating.compressors.0",
                         "property": "active"}),
        Endpoint("compressor_phase", value_type="str",
                 params={"feature": "heating.compressors.0",
                         "property": "phase"}),
        Endpoint("hours", value_type="float",
                 params={"feature": "heating.compressors.0.statistics",
                         "property": "hours"}),
        Endpoint("starts", value_type="int",
                 params={"feature": "heating.compressors.0.statistics",
                         "property": "starts"}),
        Endpoint("slope", value_type="float",
                 params={"feature": "heating.circuits.2.heating.curve",
                         "property": "slope"}),
        Endpoint("shift", value_type="float",
                 params={"feature": "heating.circuits.2.heating.curve",
                         "property": "shift"}),
        Endpoint("pump", value_type="str",
                 params={"feature": "heating.circuits.2.circulation.pump",
                         "property": "status"}),
    ], service=service)

    _tick(device)

    assert device.get("outside") == 14.4
    assert device.get("sensor_status") == "connected"
    assert device.get("compressor_active") is True
    assert device.get("compressor_phase") == "heating"
    assert device.get("hours") == 5903.7
    assert device.get("starts") == 3078
    assert device.get("slope") == 1.1
    assert device.get("shift") == 0
    assert device.get("pump") == "on"
    # One call served all nine endpoints.
    assert service.fetches == 1


def test_property_defaults_to_value():
    """An endpoint that names no property reads the feature's `value`."""
    device = _device([
        Endpoint("cop", value_type="float",
                 params={"feature": "heating.cop.total"}),
    ])
    _tick(device)
    assert device.get("cop") == 3


def test_unknown_feature_and_property_read_none_without_failing():
    """A mistyped feature or property is unavailable, not an I/O failure.

    It must not mark the device unhealthy: nothing is wrong with the
    connection, and a config mistake that looked like an outage would send
    the reader hunting in the wrong place.
    """
    device = _device([
        Endpoint("typo_feature", params={"feature": "heating.nope"}),
        Endpoint("typo_property",
                 params={"feature": "heating.cop.total", "property": "nope"}),
        Endpoint("no_feature_at_all", params={}),
        Endpoint("good", value_type="float",
                 params={"feature": "heating.cop.total"}),
    ])

    _tick(device)

    assert device.get("typo_feature") is None
    assert device.get("typo_property") is None
    assert device.get("no_feature_at_all") is None
    # The healthy endpoint alongside them is unaffected.
    assert device.get("good") == 3
    assert device.consume_reported_failure() is None


def test_disabled_feature_reads_none():
    """A feature the installation reports but has switched off is unavailable."""
    device = _device([
        Endpoint("solar", value_type="float",
                 params={"feature": "heating.solar.power.production"}),
    ])
    _tick(device)
    assert device.get("solar") is None


def test_cache_reused_within_cache_time():
    """A second poll inside cache_time does not call the API again."""
    service = _FakeService()
    device = _device([
        Endpoint("cop", value_type="float",
                 params={"feature": "heating.cop.total"}),
    ], service=service, cache_time="5m")

    _tick(device, now=0.0)
    _tick(device, now=1.0)

    assert service.fetches == 1
    assert device.get("cop") == 3


def test_cache_disabled_refetches():
    """cache_time 0s re-reads on every poll."""
    service = _FakeService()
    device = _device([
        Endpoint("cop", value_type="float",
                 params={"feature": "heating.cop.total"}),
    ], service=service, cache_time="0s")

    _tick(device, now=0.0)
    _tick(device, now=1.0)

    assert service.fetches == 2


def test_devices_on_one_gateway_share_a_fetch():
    """Two devices of one gateway cost one call, not two.

    One shared context, as load_system() gives every device of a system --
    that is what puts both on the same cache. PyViCare's via-gateway fetch
    returns every device's features in one response, so the heat pump and
    the gateway itself are one call between them.
    """
    service = _FakeService()
    context = {}
    pump = _device([
        Endpoint("cop", value_type="float",
                 params={"feature": "heating.cop.total"}),
    ], service=service, context=context, device_id="0")
    other = _device([
        Endpoint("outside", value_type="float",
                 params={"feature": "heating.sensors.temperature.outside"}),
    ], service=service, context=context, device_id="gateway")

    _tick(pump, other)

    assert service.fetches == 1
    assert pump.get("cop") == 3
    assert other.get("outside") == 14.4


def test_separate_gateways_do_not_share_a_fetch():
    """Devices on different gateways each need their own call."""
    service = _FakeService()
    context = {}
    first = _device([
        Endpoint("cop", value_type="float",
                 params={"feature": "heating.cop.total"}),
    ], service=service, context=context, serial="1111111111111111",
       device_key="pump-a")
    second = _device([
        Endpoint("cop", value_type="float",
                 params={"feature": "heating.cop.total"}),
    ], service=service, context=context, serial="2222222222222222",
       device_key="pump-b")

    _tick(first, second)

    assert service.fetches == 2


def test_failed_fetch_reports_and_does_not_poison_cache():
    """A failure reads None, marks the device unhealthy, and is not cached."""
    service = _FakeService(error=OSError("connection reset"))
    device = _device([
        Endpoint("cop", value_type="float",
                 params={"feature": "heating.cop.total"}),
    ], service=service, cache_time="5m")

    _tick(device, now=0.0)

    assert device.get("cop") is None
    assert not device.health.healthy
    assert "connection reset" in device.health.last_error

    # The next poll retries rather than serving the error for a whole
    # cache_time -- and once it succeeds, the value lands.
    service.error = None
    _tick(device, now=1.0)
    assert service.fetches == 2
    assert device.get("cop") == 3


def test_rate_limit_backs_off_without_further_calls():
    """A rate-limited account waits instead of spending more calls.

    Exceeding the quota blocks the account for 24 hours, so every poll
    while blocked must still report unhealthy but must not call the API.
    """
    from PyViCare.PyViCareUtils import PyViCareRateLimitError

    error = PyViCareRateLimitError({"extendedPayload": {
        "name": "RATE_LIMIT_EXCEEDED",
        "requestCountLimit": 1450,
        "limitReset": 1790880011000,
    }})
    service = _FakeService(error=error)
    device = _device([
        Endpoint("cop", value_type="float",
                 params={"feature": "heating.cop.total"}),
    ], service=service, cache_time="5m")

    _tick(device, now=0.0)
    assert service.fetches == 1
    assert "rate limit" in device.health.last_error.lower()

    # Inside the backoff window: still unhealthy, but no further call.
    _tick(device, now=1.0)
    assert service.fetches == 1
    assert not device.health.healthy


def test_backoff_holds_after_a_successful_first_poll():
    """Backoff must guard reads, not only the login.

    The login happens once and is then cached, so a device that polled
    successfully before being rate-limited is exactly the case that would
    keep spending a call per poll if the check lived only in the login
    path -- turning a window that recovers on its own into a 24-hour block.
    """
    from PyViCare.PyViCareUtils import PyViCareRateLimitError

    service = _FakeService()
    device = _device([
        Endpoint("cop", value_type="float",
                 params={"feature": "heating.cop.total"}),
    ], service=service, cache_time="0s")

    _tick(device, now=0.0)
    assert device.get("cop") == 3
    assert service.fetches == 1

    service.error = PyViCareRateLimitError({"extendedPayload": {
        "name": "RATE_LIMIT_EXCEEDED",
        "requestCountLimit": 1450,
        "limitReset": 1790880011000,
    }})
    _tick(device, now=1.0)
    assert service.fetches == 2
    assert not device.health.healthy

    # Every later poll inside the window reports but does not call.
    _tick(device, now=2.0)
    _tick(device, now=3.0)
    assert service.fetches == 2
    assert not device.health.healthy


def test_bad_credentials_name_the_fix():
    """A permanent auth failure tells the reader how to repair it."""
    from PyViCare.PyViCareUtils import PyViCareInvalidCredentialsError

    service = _FakeService(error=PyViCareInvalidCredentialsError())
    device = _device([
        Endpoint("cop", value_type="float",
                 params={"feature": "heating.cop.total"}),
    ], service=service)

    _tick(device)

    assert device.get("cop") is None
    assert not device.health.healthy
    assert "discover" in device.health.last_error
    assert "client_id" in device.health.last_error


def test_write_sends_the_declared_command():
    """A write becomes the command and parameter the endpoint names."""
    service = _FakeService()
    device = _device([
        Endpoint("dhw_target", value_type="int", writable=True,
                 params={"feature": "heating.dhw.temperature.main",
                         "command": "setTargetTemperature",
                         "param": "temperature"}),
    ], service=service)

    _tick(device)
    device.set_text("50", "dhw_target")

    assert service.writes == [
        ("0", "heating.dhw.temperature.main", "setTargetTemperature",
         {"temperature": 50}),
    ]


def test_write_infers_a_single_parameter_name():
    """An endpoint naming a one-parameter command needs no param:."""
    service = _FakeService()
    device = _device([
        Endpoint("mode", value_type="str", writable=True,
                 params={"feature": "heating.circuits.2.operating.modes.active",
                         "command": "setMode"}),
    ], service=service)

    _tick(device)
    device.set_text("standby", "mode")

    assert service.writes == [
        ("0", "heating.circuits.2.operating.modes.active", "setMode",
         {"mode": "standby"}),
    ]


def test_write_supplies_command_extras():
    """A command setting several values at once gets the rest from the config."""
    service = _FakeService()
    device = _device([
        Endpoint("slope", value_type="float", writable=True,
                 params={"feature": "heating.circuits.2.heating.curve",
                         "command": "setCurve", "param": "slope",
                         "command_extras": {"shift": 2}}),
    ], service=service)

    _tick(device)
    device.set_text("1.4", "slope")

    assert service.writes == [
        ("0", "heating.circuits.2.heating.curve", "setCurve",
         {"shift": 2, "slope": 1.4}),
    ]


def test_ambiguous_parameter_is_refused_before_calling():
    """A multi-parameter command with no param: must not guess."""
    service = _FakeService()
    device = _device([
        Endpoint("curve", value_type="float", writable=True,
                 params={"feature": "heating.circuits.2.heating.curve",
                         "command": "setCurve"}),
    ], service=service)

    _tick(device)
    device.set_text("1.4", "curve")

    assert service.writes == []
    failure = device.consume_reported_failure()
    assert failure is not None
    assert "param:" in failure
    assert "slope" in failure and "shift" in failure


def test_non_executable_command_is_refused():
    """A command the API reports as not executable is not attempted."""
    service = _FakeService()
    device = _device([
        Endpoint("compressor", value_type="bool", writable=True,
                 params={"feature": "heating.compressors.0",
                         "command": "setActive"}),
    ], service=service)

    _tick(device)
    device.set_text("on", "compressor")

    assert service.writes == []
    failure = device.consume_reported_failure()
    assert failure is not None
    assert "not executable" in failure


def test_unknown_command_names_the_alternatives():
    """A mistyped command says which ones the feature does have."""
    service = _FakeService()
    device = _device([
        Endpoint("dhw_target", value_type="int", writable=True,
                 params={"feature": "heating.dhw.temperature.main",
                         "command": "setTemperature"}),
    ], service=service)

    _tick(device)
    device.set_text("50", "dhw_target")

    assert service.writes == []
    failure = device.consume_reported_failure()
    assert failure is not None
    assert "setTargetTemperature" in failure


def test_one_failing_write_does_not_abandon_the_batch():
    """Writes after a rejected one still go out."""
    service = _FakeService()
    device = _device([
        Endpoint("bad", value_type="float", writable=True,
                 params={"feature": "heating.circuits.2.heating.curve",
                         "command": "setCurve"}),          # ambiguous
        Endpoint("good", value_type="str", writable=True,
                 params={"feature": "heating.circuits.2.operating.modes.active",
                         "command": "setMode"}),
    ], service=service)

    _tick(device)
    device.set_text("1.4", "bad")
    assert device.consume_reported_failure() is not None
    device.set_text("standby", "good")

    assert [w[2] for w in service.writes] == ["setMode"]


def test_write_invalidates_the_cache():
    """The next poll re-reads, so a clamped or ignored write shows up."""
    service = _FakeService()
    device = _device([
        Endpoint("dhw_target", value_type="int", writable=True,
                 params={"feature": "heating.dhw.temperature.main",
                         "command": "setTargetTemperature",
                         "param": "temperature"}),
    ], service=service, cache_time="5m")

    _tick(device, now=0.0)
    assert service.fetches == 1

    device.set_text("50", "dhw_target")
    before = service.fetches

    _tick(device, now=2.0)
    assert service.fetches == before + 1


def test_out_of_range_write_is_rejected_before_any_call():
    """min/max on the endpoint stops a bad value without spending a call."""
    service = _FakeService()
    device = _device([
        Endpoint("dhw_target", value_type="int", writable=True,
                 min=10, max=60, on_invalid="reject",
                 params={"feature": "heating.dhw.temperature.main",
                         "command": "setTargetTemperature",
                         "param": "temperature"}),
    ], service=service)

    _tick(device)
    with pytest.raises(ValueError):
        device.set(99, "dhw_target")

    assert service.writes == []


def test_endpoint_without_a_command_is_reported():
    """Writing an endpoint that declares no command says so."""
    service = _FakeService()
    device = _device([
        Endpoint("cop", value_type="float", writable=True,
                 params={"feature": "heating.cop.total"}),
    ], service=service)

    _tick(device)
    device.set_text("4", "cop")

    assert service.writes == []
    failure = device.consume_reported_failure()
    assert failure is not None
    assert "no command:" in failure


def test_module_yaml_loads_through_a_system_config(tmp_path):
    """The descriptor parses and its endpoint profiles reach the device.

    Constructing a device directly (as every test above does) never reads
    module.yaml, so this is the one that would catch a typo in it -- in a
    profile's feature name especially, which nothing else here exercises.
    It also checks the module-scoped credentials resolve from `modules:`,
    which a device entry is not allowed to set.
    """
    config = tmp_path / "house.yaml"
    config.write_text("""
modules:
  viessmann:
    email: someone@example.com
    password: secret
    client_id: 0123456789abcdef
    cache_time: 1m

devices:
  - id: heatpump
    module: viessmann
    name: Heat Pump
    device_id: "0"
    endpoints:
      - key: outside_temperature
        endpoint_profile: outside_temperature
      - key: dhw_target
        endpoint_profile: dhw_target_temperature
      - key: room_target
        readable: true
        writable: true
        type: int
        unit: "C"
        feature: heating.circuits.2.operating.programs.normal
        property: temperature
        command: setTemperature
        param: targetTemperature
""", encoding="utf-8")

    system = load_system(config)
    device = system.devices["heatpump"]

    # A profile carries the type, unit and feature name.
    outside = device.endpoint("outside_temperature")
    assert outside.unit == "°C"
    assert outside.value_type == "float"
    assert outside.params["feature"] == "heating.sensors.temperature.outside"

    # Including the writable one's command and range.
    target = device.endpoint("dhw_target")
    assert target.writable
    assert target.params["command"] == "setTargetTemperature"
    assert (target.min, target.max) == (10, 60)

    # An endpoint written out in full works alongside them.
    assert device.endpoint("room_target").params["command"] == "setTemperature"

    # Module-scoped credentials resolved onto the device.
    assert device.params["email"] == "someone@example.com"
    assert device.params["cache_time"] == "1m"

    service = _FakeService()
    device._config = _FakeDeviceConfig("0", service)
    scheduler = Scheduler(system.devices)
    try:
        scheduler.tick(now=0.0)
    finally:
        scheduler.close()

    assert device.get("outside_temperature") == 14.4
    assert device.get_text("outside_temperature") == "14.4 °C"


def test_a_device_gets_only_the_endpoints_it_asks_for(tmp_path):
    """module.yaml must not hand every device a heat pump's endpoints.

    Which features a device reports depends on what it is -- a gateway has
    no compressor -- so the module declares `endpoints: []` and offers
    profiles instead. Were those endpoints unconditional, a gateway would
    carry a heat pump's temperatures and report them all as unavailable.
    """
    config = tmp_path / "house.yaml"
    config.write_text("""
modules:
  viessmann:
    email: someone@example.com
    password: secret
    client_id: 0123456789abcdef

devices:
  - id: gateway
    module: viessmann
    device_id: gateway
    endpoints:
      - key: wifi_strength
        endpoint_profile: wifi_strength
""", encoding="utf-8")

    system = load_system(config)
    assert list(system.devices["gateway"].endpoints) == ["wifi_strength"]


# --------------------------------------------------------------- discover.py


def _discover_client(service=None, device_id="0"):
    """A stand-in for a logged-in PyViCare client, for the discovery CLI."""
    import types
    config = _FakeDeviceConfig(device_id, service or _FakeService())
    config.device_model = "CU401B_S"
    config.device_type = "heating"
    config.status = "Online"
    return types.SimpleNamespace(all_devices=[config])


def _discover_args(**overrides):
    import types
    args = types.SimpleNamespace(
        device=None, filter=None, raw=False, writable_only=False,
        include_disabled=False, token_file=None)
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def test_discover_features_lists_properties_and_commands(capsys):
    """The table names each property and how to write it."""
    from phc.devices.viessmann import discover

    discover.cmd_features(_discover_client(), _discover_args(),
                          discover._Counter())
    out = capsys.readouterr().out

    assert "heating.dhw.temperature.main" in out
    assert "setTargetTemperature(temperature: 10..60 step 1)" in out
    # The compressor's commands exist but the API refuses them.
    assert "[not executable]" in out
    # A property with a writing command is marked rw, a plain sensor r.
    main = next(line for line in out.splitlines()
                if line.startswith("heating.dhw.temperature.main"))
    assert " rw " in main
    outside = next(line for line in out.splitlines()
                   if line.startswith("heating.sensors.temperature.outside")
                   and " value " in line)
    assert " r " in outside


def test_discover_yaml_is_valid_and_pasteable():
    """The generated block parses as part of a device entry."""
    import contextlib
    import io

    import yaml

    from phc.devices.viessmann import discover

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        discover.cmd_yaml(_discover_client(), _discover_args(),
                          discover._Counter())

    preamble = "devices:\n  - id: heatpump\n    module: viessmann\n"
    document = yaml.safe_load(preamble + buffer.getvalue())
    endpoints = document["devices"][0]["endpoints"]

    keys = [e["key"] for e in endpoints]
    assert len(keys) == len(set(keys)), "generated keys must be unique"
    assert all(e.get("feature") for e in endpoints)

    by_key = {e["key"]: e for e in endpoints}
    target = by_key["dhw_temperature_main"]
    assert target["writable"] is True
    assert target["command"] == "setTargetTemperature"
    assert target["min"] == 10 and target["max"] == 60
    assert target["unit"] == "°C"

    # A two-parameter command says which it writes and flags the rest.
    slope = by_key["circuits_2_curve_slope"]
    assert slope["param"] == "slope"


def test_discover_yaml_skips_structured_properties():
    """A schedule cannot be one endpoint, so it is not offered as one."""
    import contextlib
    import io

    from phc.devices.viessmann import discover

    service = _FakeService({"data": [
        _feature("heating.dhw.schedule",
                 {"entries": {"type": "Schedule",
                              "value": {"mon": [], "tue": []}},
                  "active": {"type": "boolean", "value": True}}),
    ]})
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        discover.cmd_yaml(_discover_client(service), _discover_args(),
                          discover._Counter())
    out = buffer.getvalue()

    assert "property: entries" not in out
    # The feature's scalar property is still offered.
    assert "property: active" in out


def test_discover_filter_and_writable_only():
    """--filter and --writable-only narrow what is listed."""
    import contextlib
    import io

    from phc.devices.viessmann import discover

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        discover.cmd_features(
            _discover_client(),
            _discover_args(filter="heating.dhw.*", writable_only=True),
            discover._Counter())
    out = buffer.getvalue()

    assert "heating.dhw.temperature.main" in out
    assert "heating.sensors.temperature.outside" not in out
    assert "heating.cop.total" not in out


def test_discover_reads_credentials_from_a_config(tmp_path):
    """--config picks up modules.viessmann rather than retyping them."""
    from phc.devices.viessmann import discover

    config = tmp_path / "house.yaml"
    config.write_text("""
modules:
  viessmann:
    email: someone@example.com
    password: secret
    client_id: 0123456789abcdef
""", encoding="utf-8")

    found = discover.credentials_from_config(config)
    assert found["email"] == "someone@example.com"
    assert found["client_id"] == "0123456789abcdef"


def test_discover_reads_credentials_behind_an_include(tmp_path):
    """--config must cope with the shape a real system config has.

    Credentials belong in their own file, pulled in with `<<: !include`,
    and a system config of any size splits itself across !include-d
    fragments. Parsing with yaml.safe_load cannot read either tag, so it
    failed on exactly the configs this option exists for -- it has to use
    PHC's own loader.
    """
    from phc.devices.viessmann import discover

    (tmp_path / "account.yaml").write_text("""
email: someone@example.com
password: secret
client_id: 0123456789abcdef
""", encoding="utf-8")
    (tmp_path / "devices.yaml").write_text("""
- id: heatpump
  module: viessmann
  device_id: "0"
  endpoints:
    - key: cop
      endpoint_profile: cop
""", encoding="utf-8")
    config = tmp_path / "house.yaml"
    config.write_text("""
modules:
  viessmann:
    <<: !include account.yaml
    cache_time: 5m

devices:
  - id: viessmann
    module: host
    name: Viessmann
    children: !include devices.yaml
""", encoding="utf-8")

    found = discover.credentials_from_config(config)
    assert found["email"] == "someone@example.com"
    assert found["password"] == "secret"
    assert found["client_id"] == "0123456789abcdef"


def test_discover_reports_an_unreadable_config(tmp_path, capsys):
    """A broken config says so instead of failing obscurely later."""
    from phc.devices.viessmann import discover

    config = tmp_path / "house.yaml"
    config.write_text("modules:\n  viessmann:\n    <<: !include nope.yaml\n",
                      encoding="utf-8")

    assert discover.credentials_from_config(config) == {}
    assert "cannot read" in capsys.readouterr().err


def test_discover_requires_credentials(capsys):
    """Running with nothing to log in with says what is missing."""
    from phc.devices.viessmann import discover

    with pytest.raises(SystemExit):
        discover.main(["features"])
    assert "missing credential" in capsys.readouterr().err
