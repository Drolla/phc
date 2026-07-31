# AI skill: zway device profile generator

## Purpose

Given a description of one or more Z-Wave devices, produce an
`endpoint_profiles:`/`device_profiles:` YAML snippet for the `zway` module.
Display the snippet in chat only -- do not add it to
[`devices/zway/module.yaml`](../../devices/zway/module.yaml) or any other
file.

## References

Consult these two files:

- [`devices/zway/module.yaml`](../../devices/zway/module.yaml) -- existing
  `endpoint_profiles`/`device_profiles` to reuse, their shape, and the
  allowed `device_profiles` metadata keys (`brand`, `type`, `product`,
  `description`, `endpoints`).
- [`docs/zway.md`](../zway.md) -- `command_group` values, the `address`
  format, and how endpoint/device profiles combine.
- If the device description does not provide enough information to
  determine an endpoint's command_group or address, the skill must gather
  information from one of the following catalogs:
  - Z-Wave Alliance Certified Product Catalog:
    https://products.z-wavealliance.org/
  - Z-Wave JS Device Configuration Database:
    https://github.com/zwave-js/node-zwave-js/tree/master/packages/config/config/devices
  - OpenZWave XML Device Database:
    https://github.com/OpenZWave/open-zwave/tree/master/config

Do not check other files from this project/repo.

## Rules

- Reuse an existing endpoint_profile when its command group and access
  pattern (type/writable/unit/values) already match; only define a new one
  when nothing fits.
- `command_group` should normally be one of the values listed in
  [`docs/zway.md`](../zway.md) -- those are the only groups `thc_zWay.js`
  currently implements. If a device has other capabilities that don't fit
  any of them, the skill may propose a new group, but must mark it in the
  output with a YAML comment flagging it as unimplemented on the
  controller side and requiring a `thc_zWay.js` update before it will
  work.
- Units, ranges, or addresses may be guessed when the device description
  and the catalogs above don't strongly imply them. Mark every guessed
  value with a YAML comment flagging it for review, even though this
  deviates from the comment-free style of existing entries.
- Output must be valid YAML, styled like the existing entries in
  [`devices/zway/module.yaml`](../../devices/zway/module.yaml).
- New endpoint_profile names and device_profile keys follow the rules
  already used in
  [`devices/zway/module.yaml`](../../devices/zway/module.yaml).
- Value overrides are allowed only when explicitly provided by the user.
  The skill must not infer overrides.
- Writable must be inherited from the endpoint_profile unless the user
  explicitly overrides it.
- Addresses must follow the zway "node.instance[.datarecord]" shape. The
  skill must reject malformed addresses.
- `description` and `endpoints` are required for every device_profile;
  `brand`, `type`, and `product` are included when known and omitted
  otherwise, matching existing entries in
  [`devices/zway/module.yaml`](../../devices/zway/module.yaml).
- When multiple devices are provided, output all of them in one YAML
  block, preserving user order. Deduplicate endpoint_profiles across the
  whole output.
- If a device uses a command_group that exists but with different
  semantics, the skill must create a new endpoint_profile.
- If the device description contradicts itself or uses an unsupported
  command_group, the skill must stop and ask for clarification.
