# Web UI

[`phc/extensions/web_ui/`](../phc/extensions/web_ui/) is a small aiohttp.web server
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

The rows are checked in order, so `type: bool` wins over `values`: a
two-state endpoint declaring both (the [recommended
shape](concepts.md#two-state-endpoints)) gets a toggle, while its labels
still supply the text shown beside it.

Layout is either a single flat page (the `selectors` shorthand, default
everything) or an explicit `pages:` list, each holding one or more
collapsible `sections:` (folded by default) that pick their devices via
the same selector syntax [`phc/extensions/logdb`](logdb.md) uses:

```yaml
extensions:
  web_ui:
    home:
      host: 0.0.0.0
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
[`phc/extensions/logdb`](logdb.md) instance:

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
as [`phc/extensions/logdb`](logdb.md)'s own `selectors`) — each must also be covered by the
referenced logdb instance, or its series is empty. `title` defaults to
`id`. `unit` (optional) labels the Y axis and, when the panel's numeric
series span more than one unit (see below), names which of them gets the
left axis. `window` (default `24h`) sets the chart's initial zoom; the
full retained history is still fetched and pannable via the range
selector. `decimation` (optional) is a list of `{older_than, factor}`
tiers: samples older than `older_than` are collapsed in groups of
`factor`, bounding how much history data is shipped to the browser as it
grows. Numeric series are averaged.

`type: bool` endpoints instead get their own stacked on/off lane
(stepped, labelled by endpoint name, decimated via the endpoint's
[`aggregation`](concepts.md) rather than averaging) and move numeric
series in the panel to a right-hand axis. `binary_aggregation`
(`avg`/`min`) and `binary_lanes` (`true`/`false`) override this per
panel; there's no per-series form.

If a panel has no `type: bool` endpoints but its numeric series still
declare more than one `unit:` (e.g. temperature and humidity on one
chart), they are likewise split across the left/right axes, grouped by
their own endpoints' `unit:` — not by the panel's `selectors` order. The
panel's own `unit:`, if set, picks which group goes on the left; a group
with no declared `unit:` (e.g. a dimensionless ratio) counts as its own
group rather than merging into whichever numbered axis happens to be
first. Only two axes exist, so a panel mixing three or more units puts
the extras on the right axis together — split into separate panels if
that reads badly. This grouping only applies when the panel has no
`type: bool` lanes; a laned panel's left axis is already lane baselines,
so its numeric series always share the single right-hand axis regardless
of unit.

`kind: timers` renders a create/edit/delete UI for user timers, backed by a
named [`phc/extensions/timer`](timer.md) instance:

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

There is no authentication — `host` defaults to `0.0.0.0` (all interfaces)
for LAN reachability out of the box; only run this on a trusted LAN, and
set `host` to `127.0.0.1` to restrict it to the local machine. See
[`examples/web_ui_system.yaml`](../examples/web_ui_system.yaml) for a
complete runnable example,
[`examples/logdb_system.yaml`](../examples/logdb_system.yaml) for `kind:
graph` paired with the `logdb` instance it charts, and
[`examples/timer_system.yaml`](../examples/timer_system.yaml) for `kind:
timers` paired with the `timer` instance it manages.
