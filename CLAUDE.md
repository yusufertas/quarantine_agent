# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A control plane that contains misbehaving LLM agents. Agents run as **separate worker
processes** and ask this service for permission before each tool call; it answers
`ALLOW` / `DENY` / `FREEZE`.

**Read these before changing anything structural** — the design is settled and the
reasoning is not re-derivable from the code:

- [`docs/prd/2026-09-17-agent-quarantine.md`](docs/prd/2026-09-17-agent-quarantine.md) — scope, users, user stories, open questions
- [`docs/adr/0001-control-plane-with-cooperative-gate.md`](docs/adr/0001-control-plane-with-cooperative-gate.md) — topology and the trust model
- [`docs/adr/0002-graded-quarantine-ladder.md`](docs/adr/0002-graded-quarantine-ladder.md) — why quarantine is a ladder
- [`docs/adr/0003-tier-llm-judge-by-tool-risk.md`](docs/adr/0003-tier-llm-judge-by-tool-risk.md) — why the judge is tiered

**Status: contract suite green (182/182); runnable.** The domain, gate, judge tiering,
reaper, HTTP surface (worker protocol + human-only operator endpoints), persistence
(`SqliteRepository`, `Settings.from_env`), and the worker SDK are all implemented against
the spec-first test suite. Real gaps that remain: nothing schedules `Reaper.sweep()` on a
loop, background judging only fires when a scheduler is injected (`main.build()` supplies
one; tests deliberately do not), and no HTTP transport ships with the SDK — the developer
supplies a `post(path, body) -> dict`.

## Architecture invariants

These are load-bearing. Breaking one silently defeats the system's purpose.

1. **The state machine is the only writer of state transitions.** Not the gate, not the
   rules, not the judge. They produce verdicts; the state machine decides and records
   cause and actor. Ad-hoc `run.state = ...` anywhere else is a bug.

2. **The worker SDK is not a trust boundary.** It is a convenience wrapper over the HTTP
   protocol. Every safety property must hold when a worker bypasses it — which is what the
   heartbeat reaper exists for. Never move a check into the SDK that the server relies on.

3. **The tool risk registry is config, not data.** Version-controlled, code-reviewed. It
   answers three separate questions (judge placement, `DEGRADED` permissions, failure
   behaviour) and an **unclassified tool is `HIGH` and must fail loudly** — never a
   permissive default.

4. **The `LOW`-risk gate path must stay free of I/O.** It sits on every tool call. The
   tiering in ADR-0003 is pointless the moment that branch acquires a model call or a
   network hop.

5. **Escalation is automatic, one step at a time; de-escalation is a separate authorised
   action.** They are not symmetric operations and must not share a code path.

6. **The event log is append-only.** It is simultaneously the judge's input, the operator's
   inspection surface, and the audit trail. Nothing edits or deletes rows.

7. **This service never sees the agent's reasoning.** No prompts, no model calls on the
   agent's behalf. It is a policy decision point, not an agent framework. Requests to
   "just have the control plane also run the agent" should be pushed back on.

## Working in this repo

Follow the global workflow in `~/.claude/CLAUDE.md`. Specific to here:

- Specs and plans go in `docs/superpowers/specs/` and `docs/superpowers/plans/`.
- A new hard-to-reverse decision that emerged from a real trade-off gets an ADR
  (`docs/adr/NNNN-slug.md`). Extending an existing one? Amend it rather than adding a
  near-duplicate.
- The PRD's §8 questions are all resolved, and the resolutions are load-bearing (release
  authority, the fail-open/closed tiering, the judge being binding). If a new open question
  appears, settle and record it before building past it — don't pick an answer silently.

## Commands

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"   # setup
.venv/bin/python -m pytest                                    # full suite
.venv/bin/python -m pytest tests/test_gate.py                 # one file
.venv/bin/python -m pytest tests/test_gate.py::TestDecisionMatrix::test_every_cell
.venv/bin/python -m pytest -k "flap or recovery"               # by name
QUARANTINE_OPERATOR_TOKEN=dev .venv/bin/uvicorn quarantine.main:build --factory # run the service
```

The suite is now **green**: all 182 tests pass. Persistence (`SqliteRepository`,
`Settings.from_env`) and the worker SDK are implemented, and the service starts via
`quarantine.main:build` (not `quarantine.api:create_app`, which takes injected
collaborators directly and bypasses the env wiring and operator-token check
`main.build()` performs). Nothing in the current contract exercises code paths beyond
what's tested, so a failure anywhere means something is genuinely broken.

## Tracker

**Not yet chosen.** Per the global conventions this project must name its issue tracker
here and `@`-import that tracker's fragment (`@~/.claude/openproject.md` or
`@~/.claude/eseye-jira.md`) before any work package is created. The PRD's epic and eight
user stories are ready to publish once it is decided. **Ask before creating tracker items.**

## Documentation

**Not yet chosen.** Per the global conventions this project must declare where feature
pages are published (wiki, collection, parent category) before `document-feature` can run.
As an internal tool with no client audience, one internal destination is likely sufficient.
