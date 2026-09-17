# ADR-0002: Quarantine is a graded ladder, not a boolean

- **Status:** Accepted
- **Date:** 2026-09-17
- **Relates to:** [ADR-0001](0001-control-plane-with-cooperative-gate.md),
  [ADR-0003](0003-tier-llm-judge-by-tool-risk.md)

## Context

"Quarantine" reads as a binary — an agent is either loose or contained. Modelling it
that way is tempting because it makes the state trivial and the API obvious.

It is also wrong for this problem. Our detectors are not certain. Deterministic rules
fire on proxies (a token budget, a repeated call, an error streak) and an LLM judge
returns a fallible opinion. A boolean forces every one of those signals through a single
irreversible-feeling decision: stop the agent completely, or do nothing. In practice that
produces one of two failure modes — thresholds set loose enough that nothing ever trips
(the mechanism is decorative), or tight enough that healthy agents get killed and
operators start ignoring it.

The signals also differ in *kind*, not just strength. "This agent has burned 80% of its
budget" and "this agent appears to be pursuing a goal it was not given" call for
different responses, and neither is well served by "kill it."

## Decision

**A run occupies exactly one state on an ordered ladder:**

```
HEALTHY  →  DEGRADED  →  FROZEN  →  TERMINATED
```

- **`HEALTHY`** — normal operation; all tools permitted.
- **`DEGRADED`** — the run continues, but only `LOW`-risk tools are permitted. `HIGH`-risk
  calls return `DENY` and the agent may react to the refusal and keep working.
- **`FROZEN`** — the run is suspended. Every gate call returns `FREEZE`; the worker stops
  and its trajectory is preserved for inspection.
- **`TERMINATED`** — the run is over and cannot be resumed.

**Escalation is automatic and one step at a time. De-escalation is a separate, explicitly
authorised action** — the state machine is the only component permitted to write
transitions, and every transition records its cause and actor.

Single-dimension ordering was chosen over independent freeze/capability axes. The
expressive version (frozen *at* full capability, resuming *into* safe mode) is
representable but multiplies the states to test for a combination we could not name a
concrete use for.

## Consequences

**We accept:**

- More states to implement and test than a boolean, and a transition table that must be
  exhaustive — including the transitions we forbid.
- `DEGRADED` only has meaning if something defines which tools are safe. This is a hard
  dependency on the tool risk registry of [ADR-0003](0003-tier-llm-judge-by-tool-risk.md);
  the two decisions do not stand alone.
- Agents must handle a `DENY` on a tool they expected to work. A `DEGRADED` agent that
  treats refusal as a fatal error gets no benefit from the state existing.

**We get:**

- A proportionate response to uncertain signals. A weak detector can escalate to
  `DEGRADED` — cheap enough to tolerate false positives — without anyone tuning it to
  avoid killing healthy runs.
- Containment that preserves work. Most misbehaviour is a wandering agent, not a hostile
  one; `DEGRADED` keeps useful work flowing while removing blast radius.
- An audit trail that shows a run's history rather than its current flag.

## De-escalation (decided 2026-09-17)

**Release authority differs by state:**

- **`FROZEN` → lower requires an authenticated human.** Nothing automatic may clear a
  freeze. An automatic system permitted to clear its own alarms will clear them, and a
  frozen run is precisely the case where someone should look.
- **`DEGRADED` → `HEALTHY` may happen automatically** after a clean interval: no rule
  firing and no adverse judge verdict for a configured window. Degrading is cheap and
  common; requiring a human for every instance would make operators resent the mechanism.

**This introduces flapping**, and the design must handle it explicitly. A run that
degrades, auto-recovers, and degrades again is not healthy — it is misbehaving
intermittently, which automatic recovery would hide indefinitely. **Repeated degradation
within a window escalates to `FROZEN` instead of recovering**, and the flap counter is a
property of the run, not of the window. Without this, automatic recovery converts a
persistent problem into a permanently invisible one.

Escalation and release remain distinct operations with distinct actors and authorisation.
They must not share an implementation.
