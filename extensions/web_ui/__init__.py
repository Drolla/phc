"""web_ui extension: a small aiohttp.web server that renders the live
device tree (server-side, via Jinja2) across configurable pages/sections,
with a widget per writable/readable endpoint automatically inferred from
its existing generic metadata -- no per-device or per-endpoint-kind UI
registration. See extensions/web_ui/extension.py."""
