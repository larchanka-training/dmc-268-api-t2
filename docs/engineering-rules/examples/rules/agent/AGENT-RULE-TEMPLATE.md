# Agent rule template

Copy this block when adding an `AGENT-*` rule (or any rule written primarily for AI agents). File name: `<RULE-ID>-<short-slug>.md`. Filled examples are in [RULES_GUIDE.md § 17.2](../../../RULES_GUIDE.md#172-filled-examples).

For code rules (`PY-*`, `TS-*`, `GO-*`, `SEC-*`) use the template in [`../README.md`](../README.md#file-template).

````markdown
# <RULE-ID> · <Rule name in imperative form>

- **Severity:** MUST | SHOULD | MAY
- **Category:** correctness | security | performance | maintainability | process

## Context

When and where the rule applies: task type, files, stage of work. State what triggers the rule.

## Rule

One checkable behavior, written with MUST / SHOULD / MAY.

## Correct behavior (example)

A short, concrete example of what the agent does: command, diff excerpt, or PR / report text.

## Incorrect behavior (example)

A short, concrete example of the violation.

## Verification

How a reviewer or a tool decides "followed / not followed": the command to run, the artifact to inspect, or the review step.
````

## Authoring checklist

- [ ] The rule describes one behavior; a reviewer can answer "followed / not followed" for a given task result.
- [ ] Every MUST has at least one verification method.
- [ ] Examples show observable actions or output, not intentions.
- [ ] No facts are invented; unknowns are marked **TBD** and added to [RULES_GUIDE.md § 19.2](../../../RULES_GUIDE.md#192-open-questions-tbd).
- [ ] The rule is added to the table in [RULES_GUIDE.md § 16.3](../../../RULES_GUIDE.md#163-mandatory-rules).
