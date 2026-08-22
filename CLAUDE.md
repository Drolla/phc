# Project instructions

## Shared conventions

See [.agentic_flowspace/README.md](.agentic_flowspace/README.md) for this
repo's shared conventions and how to use them. This is also the only home
for this repo's skills, agents, and prompts
([.agentic_flowspace/skills/](.agentic_flowspace/skills/),
[.agentic_flowspace/agents/](.agentic_flowspace/agents/)) — read and follow
them from there; do not add or duplicate one under `.claude/`.

# Model & Effort Guidance

Claude Code should choose model and effort level based on the type of task at hand, using the
rules below as a starting point. When unsure which category a task falls into, default to
Sonnet at high effort.

## Subagents

If this repo uses subagents for specific jobs (linting, test generation, doc updates, etc.),
pin their model/effort directly in the subagent's frontmatter rather than relying on this file,
e.g.:

```yaml
---
name: lint-fixer
model: haiku
effort: low
---
```

```yaml
---
name: root-cause-debugger
model: opus
effort: high
---
```

This guidance is a default, not a hard rule — feel free to override with `/model` or `/effort`
for any specific task.
