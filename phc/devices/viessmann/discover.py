"""Command-line explorer for a Viessmann installation.

Lists what an account reports -- installations, gateways, devices and every
feature of each -- and turns a selection of those features into endpoint
definitions to paste into a PHC config. Which features exist depends
entirely on the hardware, so this is how to find out what to put in
`endpoints:` rather than guessing from somebody else's heat pump.

    python -m phc.devices.viessmann.discover --config house.yaml login
    python -m phc.devices.viessmann.discover --config house.yaml topology
    python -m phc.devices.viessmann.discover --config house.yaml features
    python -m phc.devices.viessmann.discover --config house.yaml features \
        --device 0 --filter "heating.dhw.*"
    python -m phc.devices.viessmann.discover --config house.yaml yaml \
        --device 0 --filter "heating.dhw.*"

Credentials come either from a PHC config file's `modules.viessmann:`
section (--config) or from --email/--password/--client-id.

Mind the quota: the free API tier allows 120 calls per 10 minutes and 1450
per day, and exceeding either blocks the account for 24 hours. Every run
prints how many calls it made. `login` makes none -- it only authenticates
-- so it is the safe one to repeat while sorting out credentials.
"""

import argparse
import fnmatch
import json
import logging
import sys

try:                                        # run as a module
    from phc.devices.viessmann.device import _by_feature
except ImportError:                         # run as a plain script
    from device import _by_feature  # type: ignore[no-redef]

from PyViCare.PyViCare import PyViCare
from PyViCare.PyViCareUtils import (
    PyViCareInvalidConfigurationError,
    PyViCareInvalidCredentialsError,
    PyViCareRateLimitError,
)

# How the API's own unit names map onto what PHC endpoints display.
UNITS = {
    "celsius": "°C",
    "kelvin": "K",
    "bar": "bar",
    "percent": "%",
    "hour": "h",
    "second": "s",
    "minute": "min",
    "watt": "W",
    "kilowatt": "kW",
    "watthour": "Wh",
    "kilowatthour": "kWh",
    "kilowattHour": "kWh",
    "cubicMeter": "m³",
    "liter": "l",
}


class _Counter:
    """Counts the API calls one run makes, so a run's cost is visible."""

    def __init__(self):
        self.calls = 0

    def wrap(self, oauth_manager):
        """Count every GET the PyViCare client performs."""
        original = oauth_manager.get

        def counting_get(url):
            self.calls += 1
            return original(url)

        oauth_manager.get = counting_get
        return oauth_manager


def connect(args, counter):
    """Log in and return a PyViCare client with its devices loaded."""
    client = PyViCare()
    client.loadViaGateway(True)
    client.setCacheDuration(0)
    manager_holder = {}

    # Count calls by wrapping the oauth manager the moment it is built,
    # before __loadInstallations spends its first call.
    original_init = client.initWithExternalOAuth

    def init(manager):
        manager_holder["manager"] = counter.wrap(manager)
        return original_init(manager)

    client.initWithExternalOAuth = init
    client.initWithCredentials(
        args.email, args.password, args.client_id, args.token_file)
    return client


def devices_of(client, only=None):
    """Return the account's devices, optionally just one by id."""
    return [d for d in client.all_devices
            if only is None or str(d.device_id) == str(only)]


def cmd_login(client, args, counter) -> int:
    """Confirm the credentials work. Makes no feature calls."""
    print("Login succeeded.")
    print(f"{len(client.all_devices)} device(s) visible on this account.")
    if args.token_file:
        print(f"Token cached in {args.token_file}")
    return 0


def cmd_topology(client, args, counter) -> int:
    """Print the installation/gateway/device ids a config needs."""
    if args.raw:
        print(json.dumps([{
            "installation_id": d.accessor.id,
            "gateway_serial": d.accessor.serial,
            "device_id": d.device_id,
            "model": d.device_model,
            "type": d.device_type,
            "status": d.status,
        } for d in client.all_devices], indent=2))
        return 0

    print(f"{'INSTALLATION':<14} {'GATEWAY':<18} {'DEVICE':<18} "
          f"{'MODEL':<20} {'TYPE':<16} STATUS")
    print("-" * 104)
    for device in client.all_devices:
        print(f"{device.accessor.id:<14} {device.accessor.serial:<18} "
              f"{str(device.device_id):<18} {str(device.device_model):<20} "
              f"{str(device.device_type):<16} {device.status}")
    print()
    print("Use these as installation_id, gateway_serial and device_id in your "
          "PHC config. device_id alone is enough unless the account has "
          "several gateways.")
    return 0


def _properties_of(entry):
    """Yield one (name, property) pair per scalar property of a feature.

    Properties holding a structure -- a heating schedule's week of time
    slots, say -- are skipped: a PHC endpoint holds one scalar, so these
    cannot be expressed as one and would render as unreadable text. The
    feature still appears if it has scalar properties alongside them.
    """
    for name, prop in (entry.get("properties") or {}).items():
        if not isinstance(prop, dict) or "value" not in prop:
            continue
        if isinstance(prop["value"], (dict, list)):
            continue
        yield name, prop


def _commands_of(entry):
    """Yield (name, spec) for each command, executable ones first."""
    commands = entry.get("commands") or {}
    return sorted(commands.items(), key=lambda kv: not kv[1].get("isExecutable"))


def _constraint_text(spec):
    """Describe one command as name(param: range) for the table."""
    parts = []
    for pname, pspec in (spec.get("params") or {}).items():
        constraints = pspec.get("constraints") or {}
        if "enum" in constraints:
            detail = "[" + ", ".join(map(str, constraints["enum"])) + "]"
        elif "min" in constraints or "max" in constraints:
            detail = f"{constraints.get('min')}..{constraints.get('max')}"
            if "stepping" in constraints:
                detail += f" step {constraints['stepping']}"
        else:
            detail = str(pspec.get("type", ""))
        parts.append(f"{pname}: {detail}")
    suffix = "" if spec.get("isExecutable") else " [not executable]"
    return f"{spec.get('name')}({', '.join(parts)}){suffix}"


def _writer_for(entry, property_name):
    """Return the command that plausibly writes this property, or None.

    A property is writable when an executable command takes a parameter of
    the same name, or when the feature has exactly one executable command
    taking exactly one parameter. Anything less obvious is reported
    read-only with its commands still listed, so the reader can see what
    exists without this guessing on their behalf.
    """
    executable = [(name, spec) for name, spec in _commands_of(entry)
                  if spec.get("isExecutable")]
    for name, spec in executable:
        if property_name in (spec.get("params") or {}):
            return name, spec, property_name
    if len(executable) == 1:
        name, spec = executable[0]
        params = list(spec.get("params") or {})
        if len(params) == 1:
            return name, spec, params[0]
    return None


def _rows(features, pattern, writable_only, include_disabled):
    """Build the table/YAML rows for one device's features."""
    rows = []
    for name in sorted(features):
        if pattern and not fnmatch.fnmatch(name, pattern):
            continue
        entry = features[name]
        if not entry.get("isEnabled", True) and not include_disabled:
            continue
        for prop_name, prop in _properties_of(entry):
            writer = _writer_for(entry, prop_name)
            if writable_only and writer is None:
                continue
            rows.append((name, prop_name, prop, entry, writer))
    return rows


def cmd_features(client, args, counter) -> int:
    """Print every feature and property, with how to write it."""
    for device in devices_of(client, args.device):
        features = _by_feature(
            device.service.fetch_all_features(device.accessor))
        if args.raw:
            print(json.dumps(list(features.values()), indent=2))
            continue

        rows = _rows(features, args.filter, args.writable_only,
                     args.include_disabled)
        enabled = sum(1 for f in features.values() if f.get("isEnabled", True))
        print(f"\ndevice {device.device_id}  ({device.device_model}, "
              f"{len(features)} features, {enabled} enabled)\n")
        if not rows:
            print("  nothing matched")
            continue

        width = max((len(name) for name, *_ in rows), default=40)
        width = min(max(width, 40), 58)
        print(f"{'FEATURE':<{width}} {'PROPERTY':<14} {'VALUE':<14} "
              f"{'UNIT':<6} {'RW':<3} COMMANDS")
        print("-" * (width + 60))
        for name, prop_name, prop, entry, writer in rows:
            unit = prop.get("unit") or ""
            value = str(prop.get("value"))
            if len(value) > 13:
                value = value[:12] + "…"
            commands = "; ".join(_constraint_text(spec)
                                 for _, spec in _commands_of(entry))
            flag = "rw" if writer else "r"
            disabled = "" if entry.get("isEnabled", True) else " [disabled]"
            shown = name if len(name) <= width else name[:width - 1] + "…"
            print(f"{shown:<{width}} {prop_name:<14} {value:<14} "
                  f"{unit:<6} {flag:<3} {commands}{disabled}")
        print(f"\n{len(rows)} row(s) shown.")
    return 0


def _key_for(feature_name, prop_name):
    """Derive a readable endpoint key from a feature and property name."""
    parts = [p for p in feature_name.split(".") if p != "heating"]
    if prop_name not in ("value",):
        parts.append(prop_name)
    key = "_".join(parts)
    # Camel case to snake, and nothing but lowercase/digits/underscore.
    out = []
    for char in key:
        if char.isupper():
            out.append("_" + char.lower())
        elif char.isalnum():
            out.append(char)
        else:
            out.append("_")
    key = "".join(out)
    while "__" in key:
        key = key.replace("__", "_")
    return key.strip("_")


def _yaml_type(prop, writer):
    """Infer a PHC endpoint type, unit and range from a live property."""
    value = prop.get("value")
    unit = UNITS.get(prop.get("unit") or "", prop.get("unit") or "")
    if isinstance(value, bool):
        return "bool", unit, {}
    if isinstance(value, (int, float)):
        kind = "int" if isinstance(value, int) else "float"
        limits = {}
        if writer:
            _, spec, param = writer
            constraints = ((spec.get("params") or {}).get(param) or {}).get(
                "constraints") or {}
            for bound in ("min", "max"):
                if bound in constraints:
                    limits[bound] = constraints[bound]
        return kind, unit, limits
    return "str", unit, {}


def cmd_yaml(client, args, counter) -> int:
    """Emit endpoint definitions to paste into a PHC config."""
    for device in devices_of(client, args.device):
        features = _by_feature(
            device.service.fetch_all_features(device.accessor))
        rows = _rows(features, args.filter, args.writable_only,
                     args.include_disabled)

        print(f"# Generated for installation {device.accessor.id}, gateway "
              f"{device.accessor.serial}, device {device.device_id} "
              f"({device.device_model}).")
        print("# Paste under a device entry's `endpoints:`. Delete what you "
              "do not want,")
        print("# shorten the keys, and replace the descriptions with your "
              "own wording --")
        print("# they are what the web UI shows.")
        if not rows:
            print("# (nothing matched)")
            continue
        print(f"    device_id: \"{device.device_id}\"")
        print("    endpoints:")
        for name, prop_name, prop, _entry, writer in rows:
            kind, unit, limits = _yaml_type(prop, writer)
            print(f"      - key: {_key_for(name, prop_name)}")
            print("        readable: true")
            print(f"        writable: {'true' if writer else 'false'}")
            print(f"        type: {kind}")
            if unit:
                print(f"        unit: \"{unit}\"")
            for bound, value in limits.items():
                print(f"        {bound}: {value}")
            if limits:
                print("        on_invalid: reject")
            if kind == "bool":
                print("        values: { false: \"off\", true: \"on\" }")
            print(f"        description: {name} ({prop_name})")
            print(f"        feature: {name}")
            print(f"        property: {prop_name}")
            if writer:
                command, spec, param = writer
                print(f"        command: {command}")
                if len(spec.get("params") or {}) > 1:
                    print(f"        param: {param}")
                    others = [p for p in (spec.get("params") or {})
                              if p != param]
                    print(f"        # setting this also needs: "
                          f"{', '.join(others)}")
                    print("        # command_extras: { "
                          + ", ".join(f"{p}: ?" for p in others) + " }")
            print()
    return 0


COMMANDS = {
    "login": cmd_login,
    "topology": cmd_topology,
    "features": cmd_features,
    "yaml": cmd_yaml,
}


def credentials_from_config(path):
    """Read `modules.viessmann:` out of a PHC system config.

    Parsed with PHC's own loader rather than yaml.safe_load, because a real
    system config puts its credentials behind `<<: !include` and splits
    itself across !include-d files -- safe_load cannot read those tags at
    all, so it failed on exactly the configs this option exists for.

    A credential still left as `!placeholder` parses to its example text,
    which is not a usable password; the login then fails with the API's own
    message. That is the same outcome as a wrong password and needs no
    special handling here.
    """
    import yaml

    try:
        from phc.core.config.yamlio import _include_stack, _IncludeLoader
        from phc.core.errors import ConfigError
    except ImportError:
        # Run as a plain script from a directory where phc is not
        # importable: fall back to safe_load, which cannot resolve
        # !include, and say so rather than failing obscurely.
        print(f"cannot read {path}: PHC is not importable here, so !include "
              f"cannot be resolved -- run this as "
              f"`python -m phc.devices.viessmann.discover` from the repo "
              f"root, or pass --email/--password/--client-id.",
              file=sys.stderr)
        return {}

    try:
        _include_stack.clear()
        with open(path, encoding="utf-8") as handle:
            raw = yaml.load(handle, Loader=_IncludeLoader) or {}
    except (yaml.YAMLError, OSError, ConfigError) as exc:
        print(f"cannot read {path}: {exc}", file=sys.stderr)
        return {}
    section = ((raw.get("modules") or {}).get("viessmann") or {})
    return {key: section.get(key) for key in
            ("email", "password", "client_id", "token_file")}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Mind the API quota: 120 calls / 10 min, 1450 / day.")
    parser.add_argument("command", choices=sorted(COMMANDS),
                        nargs="?", default="features")
    parser.add_argument("--config", metavar="FILE",
                        help="PHC config file to read modules.viessmann from")
    parser.add_argument("--email")
    parser.add_argument("--password")
    parser.add_argument("--client-id", dest="client_id")
    parser.add_argument("--token-file", dest="token_file")
    parser.add_argument("--device", metavar="ID",
                        help="only this device (default: all)")
    parser.add_argument("--filter", metavar="GLOB",
                        help='only features matching, e.g. "heating.dhw.*"')
    parser.add_argument("--writable-only", action="store_true",
                        help="only properties a command can write")
    parser.add_argument("--include-disabled", action="store_true",
                        help="also show features the installation has off")
    parser.add_argument("--raw", action="store_true",
                        help="print the API's own JSON")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s %(message)s")

    if args.config:
        for key, value in credentials_from_config(args.config).items():
            if getattr(args, key, None) is None:
                setattr(args, key, value)

    missing = [name for name in ("email", "password", "client_id")
               if not getattr(args, name, None)]
    if missing:
        parser.error(
            "missing credential(s): " + ", ".join(missing)
            + ". Give --config FILE, or --email/--password/--client-id.")

    counter = _Counter()
    try:
        client = connect(args, counter)
        result = COMMANDS[args.command](client, args, counter)
    except (PyViCareInvalidCredentialsError,
            PyViCareInvalidConfigurationError) as exc:
        print(f"login failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("Check email, password and client_id. The client id comes from "
              "https://app.developer.viessmann-climatesolutions.com",
              file=sys.stderr)
        return 1
    except PyViCareRateLimitError as exc:
        print(f"rate limit exceeded: {exc}", file=sys.stderr)
        print("Wait for the reset above before trying again -- further calls "
              "while blocked extend it.", file=sys.stderr)
        return 1

    print(f"\n{counter.calls} API call(s) made.", file=sys.stderr)
    return result


if __name__ == "__main__":
    sys.exit(main())
