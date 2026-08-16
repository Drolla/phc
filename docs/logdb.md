# Log database (logdb)

[`phc/extensions/logdb/`](../phc/extensions/logdb/) is a CSV-backed,
in-memory-buffered log of numeric device endpoint state: an
`extensions.logdb.<instance>` entry defines one log, sampled whenever a
task with `kind: log_db, instance: "logdb.<instance>"` fires. Only
`int`/`float` values are stored (`bool` is stored as `0`/`1`; `str`/other
types are skipped). Each sampled endpoint is subscribed for sticky min/max
tracking between samples, so a brief event between two samples isn't lost
— see [sticky values](scripting.md).

```yaml
extensions:
  logdb:
    house_log:
      selectors: ["house.desk_lamp/*"]
      csv_path: "logs/house_log.csv"

tasks:
  - tag: log_house
    time: "+10s"
    repeat: 1m
    action: { kind: log_db, instance: "logdb.house_log" }
```

- `selectors` (required) — a list of `"<device-glob>/<endpoint-glob>"`
  patterns (e.g. `"house.desk_lamp/*"`, `"*/*"`), or bare `"*"` for
  everything. Device globs match a device's qualified id (dot-joined, e.g.
  `"house.desk_lamp"`); `"*"` in a device glob matches across `.`
  boundaries too (i.e. matches at any depth), which is intentional.
- `csv_path` (required) — CSV file path, resolved relative to the current
  working directory.
- `full_vector_interval` (default `100`) — write a full snapshot row every
  N samples; rows in between store only changed columns. Bounds both
  restore-scan cost and how much history a header-growth event can ever
  need to sacrifice.
- `max_records` (default `100000`) — maximum records kept in memory/
  considered for restore (oldest trimmed first); `null` = unbounded by
  count.
- `max_age` (default `null`) — maximum age of kept records (a duration
  string, e.g. `"30D"`); `null` = unbounded by age.
- `header_reserve_bytes` (default `null`, meaning auto: 2x the initial
  header line length, minimum 512) — bytes reserved for the CSV label
  header line, letting it grow in place as new devices/endpoints are added
  across restarts without rewriting the whole file.

A named `logdb` instance also backs [`phc/extensions/web_ui/`](../phc/extensions/web_ui/)'s
`kind: graph` panel, which charts its logged history — see [Web UI](web-ui.md).

Not to be confused with an endpoint's own `history:` field (see [Value
history & fractiles](scripting.md#value-history--fractiles)): `logdb` is a
long-term, disk-backed, graphable series sampled by its own `log_db` task;
`history:` is a short, volatile, in-memory ring buffer read directly by a
condition/script/`set expr:`, with no CSV file and no task of its own.
