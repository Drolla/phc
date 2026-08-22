# Changelog

- Before committing changes on PHC itself (excluding any gitignored personal
  configuration) that are relevant to a developer or user of the project, record them in
  `CHANGELOG.md` under the `## [Unreleased]` section. Skip purely smaller
  or cosmetic edits (wording tweaks, UI color/spacing polish, comment
  rewording) — don't add an entry for those at all.
- Group entries by the calendar date the change was merged into `main`,
  under a `### YYYY-MM-DD` heading, most recent date first. Add to the
  existing heading for that date if one already exists in this session's
  work; otherwise add a new one above the previous most-recent date.
- Within a date, group bullets under bold category labels, only
  including the categories that actually apply that day, in this order:
  - **New features** — a wholly new capability (new extension, new YAML
    tag, new config option, new endpoint/action kind).
  - **Improvements** — a change to existing behavior that doesn't add a
    new capability (tuned defaults, better error messages/logging,
    example content changes).
  - **Bug fixes** — corrects incorrect or broken user-visible behavior.
  - **Breaking changes** — an existing YAML config may need to be
    updated to keep working (a removed/renamed parameter, changed
    semantics of an existing option).
  - **Internal changes** — implementation-only changes worth a record
    but with no user-visible effect (internal refactors, internal
    bookkeeping/lifecycle fixes, test-only changes).
- Keep example-file-only changes to one condensed bullet per date rather
  than itemizing every example tweak.
- Give the changelog update its own commit, separate from the
  code/docs/tests commits for the change it describes.
