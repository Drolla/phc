# Device Module Template

A complete, working device module to copy when adding support for a new
device. It is real running code, not a skeleton — the framework patterns in
it are the ones a production module uses, so whatever you keep is already
correct.

[`device_template/`](device_template/) simulates an "Acme Hub": a fictional
controller serving several sensor/actuator units over one connection. It
needs no hardware and no network, so you can run it before changing a line:

```
phc --config ../device_template_system.yaml
```

[`../device_template_system.yaml`](../device_template_system.yaml) is that
runnable system. Because the module sits outside the `phc` package and is
loaded through `plugin_paths:`, the pair also demonstrates shipping a module
out-of-tree.


## Copying It

1. Copy [`device_template/`](device_template/) to `phc/devices/<your_name>/`
   for a module bundled with PHC, or into a `plugin_paths:` directory to
   keep it private to one config.
2. Rename it in three places: `@register_module()`, the class name, and
   `module.yaml`'s `description`.
3. Replace `_read_payload()` and `_write_payload()` with your real protocol.
   They are the module's entire I/O surface, deliberately isolated under a
   banner comment so this is a single-site edit.
4. Delete what your device doesn't need — see the table below.
5. Remove the simulation scaffolding: the `simulate_failure` parameter,
   `_SIMULATED_UNITS`, `_hub()`, and every comment marked `SIMULATION ONLY`.


## What It Demonstrates

| Feature | Where | Keep it when |
|---|---|---|
| Reading resolved parameters | `setup()` | Always |
| `self.context` shared state | `_TemplateState` | Devices of your module share a session, cache or registry |
| Response caching + coalescing | `_get_payload()` | Several devices sit behind one controller |
| Async I/O | `receive_async`/`transmit_async` | Your I/O is async-friendly (HTTP, async SDK) |
| Blocking I/O | commented `receive`/`transmit` | Your I/O blocks (serial port, sync SDK) |
| Failure reporting | `report_failure()` in `receive_async` | You catch your own I/O errors — i.e. nearly always |
| Writes | `transmit_async()` | The device has writable endpoints |
| Per-endpoint protocol fields | `channel` + `endpoint_parameters:` | Endpoints differ by an address, column or register |
| Every parameter shape | `module.yaml` | Always — keep the ones you need, delete the rest |

`module.yaml` covers all three `override` values (`required`, `allowed`,
`none`) and both `scope`s (`device`, `module`) in one file, so you can see
each against a real example rather than in the abstract.

Endpoint and device **profiles** are the one feature not shown here: they
are mutually exclusive with the explicit `endpoints:` list this template
uses. If your module must support many similar products, see
[`phc/devices/zway/module.yaml`](../../phc/devices/zway/module.yaml) and
[`docs/profiles.md`](../../docs/profiles.md).


## Related Reading

- [`docs/developer/writing-a-device-module.md`](../../docs/developer/writing-a-device-module.md)
  — the reference explanation of everything used here
- [`docs/developer/architecture.md`](../../docs/developer/architecture.md)
  — how a device module fits into the rest of PHC
- [`tests/test_device_template.py`](../../tests/test_device_template.py)
  — a test for this module, and the pattern to copy for your own
- [`.agentic_flowspace/skills/agentic-adding-a-device-module.md`](../../.agentic_flowspace/skills/agentic-adding-a-device-module.md)
  — the end-to-end workflow, for building a module with an AI assistant
