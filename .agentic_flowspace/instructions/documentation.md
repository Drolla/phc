# Documentation

- Python code (`.py` files) shall contain developer-facing comments and
  docstrings only — internal behavior, non-obvious rationale ("why"),
  and implementation caveats. Do not write user-facing prose here.
- Every method/function (including private ones) shall have a docstring,
  however small. If a docstring has any elaboration beyond its summary,
  the summary shall be a short, single physical line, followed by a
  blank line, then the elaboration — never a summary that itself runs
  across multiple lines. A docstring that is genuinely only one short
  sentence stays a single line with no blank line. This applies to
  module, class, and function/method docstrings alike. Structure longer
  elaboration into short paragraphs (blank-line separated) rather than
  one dense block. Docstring lines shall not exceed 80 characters.
- Docstrings and comments shall use compact language: say it in as few
  words as the meaning allows. No filler phrases, and no restating
  what's already implied by names, types, or surrounding context. Prefer
  a tight fragment over a full sentence when the fragment loses nothing.
- A fact explained fully once in a module (in its header, a class
  docstring, or the first function that needs it) shall not be restated
  elsewhere in that module — later mentions get a short cross-reference
  instead.
- A docstring or comment shall not restate what the code immediately
  below it already shows. Explain the *why* (rationale, invariants,
  non-obvious consequences) rather than the *what* a reader can see in
  the next few lines.
- Docstrings follow this project's own prose convention, not
  Google-style `Args:`/`Returns:`/`Raises:` sections — parameters and
  return values stay named inline in prose.
- Module `module.yaml` files (and `extension.yaml` files) shall contain
  user-facing documentation only. Their `description` fields are
  consumed at runtime (parsed by `core/config.py`'s `ModuleDescriptor`
  and rendered as labels in the web UI), so they must stay accurate,
  plain-English explanations of what a module, parameter, or endpoint
  does — not implementation notes. Avoid bare `#` comments for internal
  caveats in these files; that content belongs in the corresponding
  `.py` docstrings instead.
- Extended user-facing documentation (concepts, configuration reference,
  profiles, per-module or per-extension deep dives, etc.) lives in
  `docs/` as topic-based Markdown files — e.g. `docs/configuration.md`,
  `docs/profiles.md`, `docs/zway.md`. Everything extended and
  user-facing goes here, whether cross-cutting or module-specific — one
  rule, one location, no exceptions carved out for individual modules.
- Extended developer-facing documentation (architecture, internals,
  guides for adding a device module or extension, etc.) lives in
  `docs/developer/` as topic-based Markdown files — e.g.
  `docs/developer/architecture.md`,
  `docs/developer/adding-a-device-module.md`, `docs/developer/zway.md`.
  Same rule as above: one location for all extended developer docs,
  cross-cutting or module-specific alike.
- `README.md` stays a concise summary covering both audiences, weighted
  toward user documentation as the main focus, linking out to `docs/`
  and `docs/developer/` for details rather than embedding them inline.
