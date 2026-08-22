# Code style

- Avoid unnecessary duplication of logic (not just duplicated
  explanation) within a module: when the same logic appears more than
  once with only minor variation, factor it into a shared helper — but
  only when that's a clear net win. Don't introduce an abstraction that
  costs more (an extra function plus call sites) than the duplication it
  removes; three similar lines is better than a premature abstraction.
- Prefer the version that reduces line count without hurting
  readability.
- Keep a change scoped to the module(s) the task actually requires
  touching. Noticing a similar improvement elsewhere doesn't mean making
  it now — flag it or ask, rather than expanding scope unprompted.
