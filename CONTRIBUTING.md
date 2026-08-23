# Contributing

Thanks for considering a contribution to PHC! This is the starting point
whether you're fixing a bug, extending the core, writing a new extension,
or adding a device module — PHC was nearly entirely developed with AI
coding assistants, and its module/extension patterns are small and
consistently templated enough that an assistant (Claude Code, Codex,
Gemini CLI, Copilot, ...) can build a new one largely on its own.
[`.agentic_flowspace/`](.agentic_flowspace/) is this repo's shared set of
conventions such an assistant should read first — see
[`.agentic_flowspace/README.md`](.agentic_flowspace/README.md).

## Development Setup

```
pip install -e ".[dev]"
pytest
ruff check phc tests examples/device-template
mypy
```

All three run in CI. `ruff` and `mypy` are configured in `pyproject.toml`.
`examples/device-template` is linted alongside the package because it is
code people copy — see [Adding a Device Module or
Extension](#adding-a-device-module-or-extension).

`mypy` is a **ratchet**: the codebase was never type-checked, so the
modules that still have findings are listed as exempt under
`[[tool.mypy.overrides]]`. Everything else must stay clean. Tighten by
deleting a name from that list and fixing what it then reports — adding
one back should need a reason.


## Workflow

- Work on a dedicated branch — never commit directly to `main`.
- Merge into `main` without fast-forward (`git merge --no-ff`), so each
  contribution stays visible as a single merge commit in history.
- Split unrelated concerns into separate commits — e.g. one commit for
  code, one for docs, one for example YAML configs, one for tests — rather
  than bundling everything into one.
- Add or update tests for any behavior change; `pytest` must pass before
  opening a pull request.


## Documentation

- Python docstrings/comments are for developers: internal behavior, non-
  obvious rationale, implementation caveats. Every function/method gets a
  docstring, however small.
- A `module.yaml`/`extension.yaml`'s `description` fields are user-facing
  (rendered in the web UI) — plain-English explanations, not implementation
  notes.
- Extended user-facing documentation goes in [`docs/`](docs/); extended
  developer documentation (architecture, internals, guides for adding a
  device module or extension) goes in [`docs/developer/`](docs/developer/).
- `README.md` stays a concise summary linking out to `docs/` and
  `docs/developer/` rather than embedding details inline.


## Adding a Device Module or Extension

For a device module, start from
[`examples/device-template/`](examples/device-template/): a complete,
working module written to be copied, which runs with no hardware or
network and demonstrates every pattern in one place. Copy it, swap in your
protocol, and delete what you don't need.

See [`docs/developer/architecture.md`](docs/developer/architecture.md) for
how the pieces fit together,
[`writing-a-device-module.md`](docs/developer/writing-a-device-module.md)
for the reference behind that template, and
[`writing-an-extension.md`](docs/developer/writing-an-extension.md) for the
extension.yaml + extension.py pattern.

A descriptor (`module.yaml`/`extension.yaml`) and any web assets are
package *data*, not code, so a new one must also be covered by
`[tool.setuptools.package-data]` in `pyproject.toml` — otherwise it works
in a source checkout and is missing from every real install. The
`wheel-install` CI job checks this.
