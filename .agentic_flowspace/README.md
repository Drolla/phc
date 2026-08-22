# How to use agentic_flowspace

This repository defines its project conventions once, here, and shares them
across coding-assistant tools (Claude Code, Codex, Gemini CLI, Copilot).
Before making changes:

1. Read `index.json` — it lists every shared instruction (and, over time,
   skill and agent) file with a one-line description.
2. Read every file listed under `instructions/` and follow it exactly as if
   it were written in your own instructions file — it covers git workflow,
   documentation, code style, and changelog conventions for this repo.
3. If `skills/` or `agents/` list any entries, read their descriptions too:
   they describe specialized workflows for particular kinds of tasks. Claude
   Code has a native mechanism to execute a matching skill/subagent
   (`.claude/skills/`, `.claude/agents/`) when one is defined there; other
   tools have no built-in invocation mechanism and should follow the
   guidance manually, as part of normal reasoning, when a task matches.

PHC itself is developed nearly entirely with AI coding assistants, using
this shared setup rather than one-off prompting. See
[`docs/developer/agentic-creating-a-skill.md`](../docs/developer/agentic-creating-a-skill.md)
for how a new `skills/` workflow gets drafted with an assistant, and
[`docs/developer/agentic-adding-a-device.md`](../docs/developer/agentic-adding-a-device.md)
for a worked example of the `agentic-adding-a-device-module` skill
scaffolding a new device module end to end.
