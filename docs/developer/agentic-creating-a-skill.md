# Writing a skill

A skill under `.agentic_flowspace/skills/` doesn't have to be written by
hand. `agentic-adding-a-device-module` was built entirely through a conversation
with Claude Code. Below is that conversation as a worked example,
condensed and cleaned up.

## 1. Ask for the right mechanism first

> I'd like AI support for adding a new device to PHC. Should this be a
> prompt, an instruction, a skill, or an agent?

Claude read `.agentic_flowspace/README.md` and the (still empty)
`skills/`/`agents/` folders, then looked at
[writing-a-device-module.md](writing-a-device-module.md), the existing
human-facing pattern doc. It recommended a skill: the domain knowledge
was already written down, so what was missing was a standing definition
of *when* and *how* to walk through it — more durable and discoverable
than a one-off prompt, not a background rule the way `instructions/` is,
and not enough need for isolated context to justify a subagent. It named
that tradeoff and asked whether to build one.

## 2. Build it from the existing documentation

> Yes, create this skill using the available documentation in docs. It
> should ask the user which physical device should be the endpoints, and
> propose to implement an example configuration that demonstrates the use
> of the device.

Claude read `writing-a-device-module.md` in full, alongside a working
example of everything it describes: `module.yaml` from `virtual` and
`meteoswiss`, the `meteoswiss_stations.yaml` / `meteo_multi_city.yaml`
example configs, `test_meteoswiss.py` for the test pattern, the
`pyproject.toml` package-data wildcard, and this repo's git-workflow and
changelog instructions. It then wrote
`.agentic_flowspace/skills/agentic-adding-a-device-module.md`, registered it in
`index.json`, and logged the addition in `CHANGELOG.md`.

## 3. Add a missing clarifying question

> Ask the user also whether the device shall be stored within the PHC
> project or repo, or outside.

The first draft asked which endpoints the device needs, but not where
the module itself should live. Claude added that as a second clarifying
question next to it, since it decides where the module actually gets
scaffolded (`phc/devices/<name>/` vs. an out-of-tree package).

## 4. Trim it

> Reduce the text you've added — this is too verbose.

One line was enough to cut the two additions from step 3 down to match
the density of the rest of the skill.

## Takeaways

- Ask the assistant to recommend prompt vs. instruction vs. skill vs.
  agent before writing anything — the answer usually falls out of how the
  repo's own conventions are already organized.
- Point it at whatever documentation the skill should be built from, so
  it cross-references that material instead of duplicating it.
- Review the draft for what's missing, not just what's wrong — a missing
  clarifying question is as real a defect as an incorrect step.
- A single "too verbose, trim it" is often all it takes to fix an LLM's
  tendency to over-explain.
