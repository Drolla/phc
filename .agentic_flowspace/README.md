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
