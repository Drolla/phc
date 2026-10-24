"""Command-line demo for the Matter server WebSocket client ./matter_ws.py.

Lists nodes, reads attributes, switches and labels nodes, sends raw
commands and prints live events. It can be run either as a module or as a
plain script from this directory:

    python demo.py list
    python demo.py get "Living lamp"
    python demo.py get "Living lamp" 1/6/0
    python demo.py on "Living lamp"
    python demo.py label 3 "Living lamp"
    python demo.py raw get_nodes
    python demo.py raw device_command '{"node_id": 3, "endpoint_id": 1,
        "cluster_id": 6, "command_name": "Toggle", "payload": {}}'
    python demo.py listen

--url defaults to the server on this machine, ws://127.0.0.1:5580/ws.
NODE is the same label/serial glob (or node id) the YAML config takes, so
this is also how to find out what to put there. Attribute paths are
"endpoint/cluster/attribute" in decimal, e.g. 1/6/0 for On/Off."""

import argparse
import asyncio
import json
import logging
import sys

from matter_ws import (
    LABEL_PATH,
    MatterConnection,
    MatterError,
    write_for,
)

PRODUCT_PATH = "0/40/3"
ON_OFF_COMMANDS = {"commands": {True: "On", False: "Off"}}
ON_OFF_CLUSTER = 6


def parse_node(text: str) -> int | str:
    """A bare number is a node id; anything else is a glob."""
    return int(text) if text.isdigit() else text


def endpoint_clusters(attributes: dict) -> dict[int, list[int]]:
    """Map each endpoint of a node to the clusters it implements."""
    clusters: dict[int, set[int]] = {}
    for path in attributes:
        endpoint, cluster, _ = (int(part) for part in path.split("/"))
        clusters.setdefault(endpoint, set()).add(cluster)
    return {endpoint: sorted(found) for endpoint, found in sorted(clusters.items())}


async def cmd_list(connection: MatterConnection, args) -> int:
    """Print every node with its label, product and endpoints."""
    print(f"{len(connection.nodes)} node(s):\n")
    for node_id, node in sorted(connection.nodes.items()):
        attributes = node["attributes"]
        state = "available" if node["available"] else "UNAVAILABLE"
        print(f"node {node_id}: {attributes.get(LABEL_PATH) or '(no label)'}"
              f" | {attributes.get(PRODUCT_PATH, '?')} | {state}")
        for endpoint, found in endpoint_clusters(attributes).items():
            print(f"    endpoint {endpoint}: clusters {', '.join(map(str, found))}")
    return 0


async def cmd_get(connection: MatterConnection, args) -> int:
    """Print one attribute of a node, or all of its attributes."""
    node_id = connection.resolve(parse_node(args.node), args.match_case)
    attributes = connection.nodes[node_id]["attributes"]
    if args.path:
        print(json.dumps(connection.read(node_id, args.path)))
        return 0
    print(json.dumps(attributes, indent=2, sort_keys=True))
    return 0


async def cmd_switch(connection: MatterConnection, args) -> int:
    """Switch a node on or off."""
    node_id = connection.resolve(parse_node(args.node), args.match_case)
    name, payload = write_for(args.command == "on", ON_OFF_COMMANDS)
    await connection.command(node_id, args.endpoint, ON_OFF_CLUSTER, name, payload)
    path = f"{args.endpoint}/{ON_OFF_CLUSTER}/0"
    print(f"node {node_id}: {name}")
    # The server pushes the new state; give it a moment to arrive.
    await asyncio.sleep(1.0)
    print(f"now: {connection.read(node_id, path)!r}")
    return 0


async def cmd_label(connection: MatterConnection, args) -> int:
    """Store a label on a node, for the config's `node:` to match."""
    node_id = connection.resolve(parse_node(args.node), args.match_case)
    await connection.request("write_attribute", node_id=node_id,
                             attribute_path=LABEL_PATH, value=args.text)
    print(f"node {node_id} labelled {args.text!r}")
    return 0


async def cmd_raw(connection: MatterConnection, args) -> int:
    """Send any server command and print the reply."""
    extra = json.loads(args.args) if args.args else {}
    print(json.dumps(await connection.request(args.name, **extra), indent=2))
    return 0


async def cmd_listen(connection: MatterConnection, args) -> int:
    """Print server events until interrupted."""
    connection.on_event = lambda event, data: print(json.dumps({event: data}))
    print(f"Listening ({len(connection.nodes)} nodes), Ctrl+C to stop")
    while connection.connected:
        await asyncio.sleep(0.5)
    print("connection closed", file=sys.stderr)
    return 1


COMMANDS = {
    "list": cmd_list,
    "get": cmd_get,
    "on": cmd_switch,
    "off": cmd_switch,
    "label": cmd_label,
    "raw": cmd_raw,
    "listen": cmd_listen,
}


async def main(args) -> int:
    connection = MatterConnection(args.url, request_timeout=args.timeout)
    if not await connection.ensure_started():
        print(f"cannot reach {args.url}: {connection.last_error}", file=sys.stderr)
        return 1
    try:
        return await COMMANDS[args.command](connection, args)
    except MatterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        await connection.close()


def parse_args(argv=None):
    """Parse the command line."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="ws://127.0.0.1:5580/ws",
                        help="WebSocket URL of the server (default: %(default)s)")
    parser.add_argument("--timeout", type=float, default=10.0,
                        help="Seconds to wait for one reply (default: 10)")
    parser.add_argument("--match-case", action="store_true",
                        help="Match NODE case-sensitively")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Log every request that is sent")

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="List all nodes with label, product and endpoints")
    get = sub.add_parser("get", help="Show a node's attributes, or one of them")
    get.add_argument("node", help="Label/serial glob or node id")
    get.add_argument("path", nargs="?", help="Attribute path, e.g. 1/6/0")
    for name, help_text in (("on", "Switch a node on"), ("off", "Switch a node off")):
        switch = sub.add_parser(name, help=help_text)
        switch.add_argument("node", help="Label/serial glob or node id")
        switch.add_argument("--endpoint", type=int, default=1,
                            help="Endpoint number (default: 1)")
    label = sub.add_parser("label", help="Store a label (NodeLabel) on a node")
    label.add_argument("node", help="Label/serial glob or node id")
    label.add_argument("text", help="The new label")
    raw = sub.add_parser("raw", help="Send any server command")
    raw.add_argument("name", help="Command name, e.g. get_nodes")
    raw.add_argument("args", nargs="?", help="Command arguments as a JSON object")
    sub.add_parser("listen", help="Print server events until Ctrl+C")

    return parser.parse_args(argv)


if __name__ == "__main__":
    parsed = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if parsed.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
    )
    try:
        sys.exit(asyncio.run(main(parsed)))
    except KeyboardInterrupt:
        pass
