# ADR-0003: The LLM judge runs synchronously only for high-blast-radius tools

- **Status:** Accepted
- **Date:** 2026-09-17
- **Relates to:** [ADR-0001](0001-control-plane-with-cooperative-gate.md),
  [ADR-0002](0002-graded-quarantine-ladder.md)

## Context

Two detector families escalate a run: deterministic rules over counters, and an LLM judge
that reads the recent trajectory and classifies semantic misbehaviour rules cannot express
(off-task drift, goal substitution, deception).

Their cost profiles are three orders of magnitude apart. Rules are sub-millisecond, pure
functions over state we already hold. The judge is a model call: seconds of latency, real
money per invocation, and an external dependency that can fail.

Per [ADR-0001](0001-control-plane-with-cooperative-gate.md) the gate sits on the critical
path of **every** tool call, so where the judge runs is a question about every call the
system will ever make. Three placements were considered:

- **(A) Background only** — the gate answers from counters; the judge runs out-of-band and
  its verdict lands on a later gate call. Never blocks, one code path, but creates a lag
  window in which the agent takes further actions under a stale verdict.
- **(B) Inline on suspicion** — a rule trips a suspicion flag and the gate blocks on the
  judge before answering. Closes the lag window everywhere, at the price of multi-second
  tool calls and a hard availability dependency on the judge.
- **(C) Tier by blast radius** — the placement depends on what the tool can do.

The lag window in (A) is not uniformly costly. For an agent drifting off-task, three extra
read-only calls are irrelevant. For an agent about to issue a payment or delete a table,
they are the whole problem. The cost of being late is a property of the *tool*, not of the
detector — which is the observation this decision rests on.

## Decision

**Every tool carries a risk level in a registry, and that registry decides where the judge
runs:**

- **`LOW`-risk tools** (read-only, reversible, cheap) take the fast path: deterministic
  rules only. The judge evaluates these trajectories in the background and its verdict
  applies from the next gate call onward.
- **`HIGH`-risk tools** (writes, sends, payments, deletions — anything not cheaply
  reversible) block on a synchronous judge call before the gate answers.

**The registry lives in version-controlled configuration, not the database.** Which tools
can hurt you is reviewed code, not runtime data.

**The same registry answers three questions**, and this reuse is the reason to prefer (C)
over (A) despite the extra code path:

1. Does this call need a synchronous judge? (this ADR)
2. Is this call permitted in `DEGRADED`? ([ADR-0002](0002-graded-quarantine-ladder.md) —
   `DEGRADED` has no definition without it)
3. Does this call fail open or fail closed when the control plane or judge is unreachable?

`DEGRADED` requires this table regardless. The marginal cost of (C) over (A) is therefore
a second gate path, not a second system.

## Consequences

**We accept:**

- Two gate paths to implement and test, and the fast path must stay fast — the tiering is
  pointless if the `LOW` branch acquires I/O.
- Latency and cost on high-risk tool calls. Agents that do destructive work in tight loops
  will feel it, and that is intended.
- **An unclassified tool is a correctness bug, not a default.** A tool missing from the
  registry must be treated as `HIGH` and must fail loudly in tests, or the registry
  silently degrades into a permit-list for anything new.
- Judge unavailability now affects agent progress on high-risk calls. The fail-open /
  fail-closed rule must be decided per risk level, not globally.

**We get:**

- Latency and spend concentrated where a wrong decision is expensive, and absent everywhere
  else.
- One reviewable artifact — the registry — that answers "what can this agent do to us",
  which is the question an operator actually asks.

**Rejected because:** (A) leaves the lag window open precisely where it is unaffordable.
(B) pays full latency and full availability risk on read-only calls that cannot cause
harm, buying safety that was not at risk.
