# Web UI

[`extensions/web_ui/`](../extensions/web_ui/) is a small aiohttp.web server
that renders the live device tree as a browser dashboard — view current
status and flip/slide/select new values — with **no per-device UI code**:
each endpoint's widget is inferred purely from its existing metadata:

| Endpoint                                    | Widget     |
|----------------------------------------------|------------|
| not `writable`                                | label      |
| `writable`, `type: bool`                      | toggle     |
| `writable`, has `values`                      | dropdown   |
| `writable`, numeric, both `min` and `max`     | slider     |
| `writable`, numeric, missing `min` or `max`   | number     |
| `writable`, `str` or untyped                  | text       |

Layout is either a single flat page (the `selectors` shorthand, default
everything) or an explicit `pages:` list, each holding one or more
collapsible `sections:` (folded by default) that pick their devices via
the same selector syntax [`extensions/logdb`](logdb.md) uses:

```yaml
extensions:
  web_ui:
    home:
      host: 127.0.0.1
      port: 8080
      refresh_interval: 2s
      pages:
        - id: overview
          title: Overview
          sections:
            - id: lights
              title: Lights
              collapsed: false
              selectors: ["house.*.light*/*"]
            - id: climate
              title: Climate
              selectors: ["house.*/temperature", "house.*/humidity"]
```

Writes POST through the same `Device.set_text()` path a task action uses;
every widget independently polls its own small HTML fragment on
`refresh_interval` to pick up live state (its own write included, once the
next scheduler tick commits it — there is no WebSocket/push channel).
Interactivity is [HTMX](https://htmx.org) and styling is
[Bootstrap](https://getbootstrap.com) (CSS only — no build tooling needed).

A section's content is a list of **panels**, dispatched by `kind` (default
`"devices"`, the widgets described above).

`kind: graph` renders a [Dygraphs](https://dygraphs.com) time-series chart
over one or more endpoints' logged history, backed by a named
[`extensions/logdb`](logdb.md) instance:

```yaml
extensions:
  logdb:
    house_log:
      selectors: ["house.desk_lamp/*"]
      csv_path: "logs/house_log.csv"

  web_ui:
    home:
      pages:
        - id: overview
          sections:
            - id: history
              title: History
              panels:
                - kind: graph
                  id: desk_lamp_history
                  logdb_instance: "logdb.house_log"
                  selectors: ["house.desk_lamp/*"]
                  title: "Desk Lamp"
                  window: 6h
                  decimation:
                    - older_than: 25h
                      factor: 3
                    - older_than: 8D
                      factor: 8
```

`id` (required, unique across this web_ui instance) addresses the panel's
own `GET /api/graph/{id}` JSON data route — fetched client-side, not
embedded in the page render. `logdb_instance` (required) is resolved
lazily, at request time, so it may be declared either before or after this
`web_ui:` instance. `selectors` picks which endpoints to plot (same syntax
as `extensions/logdb`'s own `selectors`) — each must also be covered by the
referenced logdb instance, or its series is empty. `title` defaults to
`id`. `unit` (optional) labels the Y axis. `window` (default `24h`) sets
the chart's initial zoom; the full retained history is still fetched and
pannable via the range selector. `decimation` (optional) is a list of
`{older_than, factor}` tiers: samples older than `older_than` are averaged
in groups of `factor`, bounding how much history data is shipped to the
browser as it grows.

`kind: timers` renders a create/edit/delete UI for user timers, backed by a
named [`extensions/timer`](timer.md) instance:

```yaml
extensions:
  timer:
    house:
      path: "logs/timers.yaml"
      selectors: ["house.*/*"]

  web_ui:
    home:
      pages:
        - id: overview
          sections:
            - id: timers
              title: Timers
              panels:
                - kind: timers
                  id: house_timers
                  timer_instance: "timer.house"
```

`id` (required, unique across this web_ui instance) addresses the panel's
own poll/CRUD routes. `timer_instance` (required) is resolved lazily, at
request time, same as `kind: graph`'s `logdb_instance` — it may be declared
either before or after this `web_ui:` instance. `title` defaults to `id`.
Only endpoints matched by that timer instance's own `selectors` can be
picked as a timer's target.

There is no authentication — bind `host` to a trusted interface only
(defaults to `127.0.0.1`, loopback-only). See
[`examples/web_ui_system.yaml`](../examples/web_ui_system.yaml) for a
complete runnable example,
[`examples/logdb_system.yaml`](../examples/logdb_system.yaml) for `kind:
graph` paired with the `logdb` instance it charts, and
[`examples/timer_system.yaml`](../examples/timer_system.yaml) for `kind:
timers` paired with the `timer` instance it manages.
