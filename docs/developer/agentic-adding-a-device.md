# Adding a device with an AI assistant

A new device module is templated enough for an AI assistant to build end
to end, driven by
[`agentic-adding-a-device-module`](../../.agentic_flowspace/skills/agentic-adding-a-device-module.md).
Below is a worked example — a `yahoo_finance` stock-quote device — shown
purely to illustrate the workflow; that module isn't part of this repo.
Exactly how an assistant behaves at each step (whether it asks, assumes,
or reacts differently altogether) depends on the AI model driving it.

## 1. State the goal

> Build a device that allows displaying stock prices

Invoking the skill by name failed (it's repo-local, not built in), so the
assistant read its Markdown file directly and followed it as instructions.

## 2. Clarify the physical device first (skill step 1)

Instead of one open-ended question, the assistant asked three
multiple-choice ones — data source, endpoints, symbol scope:

> free public API (no key), price + change + volume, one device per symbol

## 3. Pick the closest existing module as a template

The assistant read `writing-a-device-module.md`, then picked whichever
existing module's caching/parameter-scope shape actually matched, rather
than "following the docs" in the abstract.

## 4. Verify beyond the unit tests

It loaded the new `module.yaml` through the real registry/config path
against a throwaway system config, not just a directly-constructed
`Device` — the only way to actually confirm the YAML parses.

## 5. Confirm the example config's shape before writing it

The new device's parameter scope didn't match the example it would
otherwise have copied, so the assistant proposed an adapted shape and
asked first:

> Yes, add both files

## Takeaways

- Multiple-choice clarifying questions settle several decisions in one
  round-trip.
- Name the closest existing module as template, don't just say "follow
  the docs".
- Verify through the real registry/config loader, not a unit test alone.
- Confirm an adapted example shape before writing it, don't copy by rote.
