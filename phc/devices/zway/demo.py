"""Command-line demo for the Z-Way WebSocket client ./zway_ws.py.

Lists devices, reads a state and writes one. It can be run either as a module
or as a plain script from this directory:

    python demo.py --url URL --token TOKEN list
    python demo.py --url URL --token TOKEN list
    python demo.py --url URL --token TOKEN get "*living temp*"
    python demo.py --url URL --token TOKEN on "Rez: Light corridor"
    python demo.py --url URL --token TOKEN set "Dummy Device 2" 42

URL = ws://<HOST>:<PORT>
HOST: IP address or hostname of the Z-Way controller
PORT: usually 8083, but can be changed in the Z-Way web interface
TOKEN = API token for the controller, created in the Z-Way web interface

DEVICE is the same id/title glob the YAML config takes, so this is also
how to find out what to put there."""

import argparse
import asyncio
import json
import logging
import sys

from zway_ws import (
    DEVICES_PATH,
    ZWayConnection,
    ZWayError,
    command_for,
    to_phc,
)


async def cmd_list(connection: ZWayConnection, args) -> int:
    """Print every device with its wire level and translated value."""
    if args.raw:
        print(json.dumps(await connection.request(DEVICES_PATH), indent=2))
        return 0

    device_ids = sorted(connection.titles)
    print(f"{len(device_ids)} device(s):\n")
    print(f"{'DEVICE ID':<26} | {'TITLE':<34} | {'TYPE':<17} | WIRE -> PHC")
    print("-" * 104)
    for device_id in device_ids:
        device_type = connection.types.get(device_id, "")
        level = connection.levels.get(device_id)
        value = to_phc(level, device_type)
        print(f"{device_id:<26} | {connection.titles[device_id]:<34} | "
              f"{device_type:<17} | {level!r} -> {value!r}")
    return 0


async def cmd_get(connection: ZWayConnection, args) -> int:
    """Print one device's state."""
    device_id = connection.resolve(args.device, args.match_case)
    if args.raw:
        print(json.dumps(await connection.request(f"{DEVICES_PATH}/{device_id}"), indent=2))
        return 0

    device_type = connection.types.get(device_id, "")
    level = connection.levels.get(device_id)
    print(f"id:    {device_id}")
    print(f"title: {connection.titles.get(device_id)}")
    print(f"type:  {device_type}")
    print(f"value: {to_phc(level, device_type)!r}   (wire: {level!r})")
    return 0


async def cmd_write(connection: ZWayConnection, args) -> int:
    """Switch a device on/off, or set it to an exact level."""
    device_id = connection.resolve(args.device, args.match_case)
    device_type = connection.types.get(device_id, "")
    value = {"on": True, "off": False}.get(args.command, getattr(args, "value", None))

    command = command_for(value, device_type)
    response = await connection.command(device_id, command)
    if args.raw:
        print(json.dumps(response, indent=2))
        return 0

    print(f"{device_id} ({connection.titles.get(device_id)}): {command}")
    # The controller pushes the new level; give it a moment to arrive.
    await asyncio.sleep(1.5)
    level = connection.levels.get(device_id)
    print(f"now: {to_phc(level, device_type)!r}   (wire: {level!r})")
    return 0


COMMANDS = {
    "list": cmd_list,
    "get": cmd_get,
    "on": cmd_write,
    "off": cmd_write,
    "set": cmd_write,
}


async def main(args) -> int:
    connection = ZWayConnection(args.url, args.token, request_timeout=args.timeout)
    if not await connection.ensure_started():
        print(f"cannot reach {args.url}: {connection.last_error}", file=sys.stderr)
        return 1
    try:
        return await COMMANDS[args.command](connection, args)
    except ZWayError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        await connection.close()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True,
                        help="WebSocket URL of the controller, e.g. ws://192.168.1.132:8083")
    parser.add_argument("--token", required=True, help="API token for the controller")
    parser.add_argument("--timeout", type=float, default=5.0,
                        help="Seconds to wait for one reply (default: 5)")
    parser.add_argument("--match-case", action="store_true",
                        help="Match DEVICE case-sensitively")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Log every request that is sent")
    parser.add_argument("--raw", action="store_true",
                        help="Print the controller's JSON instead of a summary")

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="List all devices with their current state")
    for name, help_text in (("get", "Show the current state of a device"),
                            ("on", "Switch a device on"),
                            ("off", "Switch a device off")):
        subparser = sub.add_parser(name, help=help_text)
        subparser.add_argument("device", help="Device id or title (fnmatch glob)")
    set_parser = sub.add_parser("set", help="Set a device to an exact level")
    set_parser.add_argument("device", help="Device id or title (fnmatch glob)")
    set_parser.add_argument("value", help="Level to set, e.g. 42")

    return parser.parse_args(argv)


if __name__ == "__main__":
    parsed = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if parsed.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
    )
    sys.exit(asyncio.run(main(parsed)))
