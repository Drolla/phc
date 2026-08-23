# Adding a Device with an AI Assistant

A new device module is templated enough for an AI assistant to build end
to end, driven by
[`agentic-adding-a-device-module`](../../.agentic_flowspace/skills/agentic-adding-a-device-module.md).
Below is a worked example — a `yahoo_finance` stock-quote device — shown
purely to illustrate the workflow; that module isn't part of this repo.
Exactly how an assistant behaves at each step (whether it asks, assumes,
or reacts differently altogether) depends on the AI model driving it.


## 1. State the Goal

> Build a device that allows displaying stock prices

Invoking the skill by name failed (it's repo-local, not built in), so the
assistant read its Markdown file directly and followed it as instructions.


## 2. Clarify the Physical Device First (Skill Step 1)

Instead of one open-ended question, the assistant asked three
multiple-choice ones — data source, endpoints, symbol scope:

> free public API (no key), price + change + volume, one device per symbol


## 3. Pick the Closest Existing Module as a Template

The assistant read [`writing-a-device-module.md`](writing-a-device-module.md), then picked whichever
existing module's caching/parameter-scope shape actually matched, rather
than "following the docs" in the abstract.

> **This step has since changed.** Copying whichever module looked closest
> was the weak point of this workflow: the modules differed, and one of the
> shapes on offer was a caching pattern the documentation elsewhere
> forbids. There is now a single canonical
> [`examples/device-template/`](../../examples/device-template/) to copy
> instead, and the skill's step 3 says so. The rest of this walkthrough
> still applies.


## 4. Verify Beyond the Unit Tests

It loaded the new `module.yaml` through the real registry/config path
against a throwaway system config, not just a directly-constructed
`Device` — the only way to actually confirm the YAML parses.


## 5. Confirm the Example Config's Shape Before Writing It

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
