# ADR-0001: Quarantine enforcement is a cooperative gate against a separate control plane

- **Status:** Accepted
- **Date:** 2026-09-17
- **Supersedes / superseded by:** —

## Context

Agents are LLM tool-calling loops that we own and run. When one misbehaves we need to
stop it mid-run, but "stopping" a running agent can be done at very different layers,
and the layer we pick is effectively permanent — it determines the deployment topology,
what state we can capture, and whether the safety property survives a bad worker.

Four layers were considered:

1. **In-process** — runs are async tasks inside the FastAPI app; freezing means the loop
   reads a shared state object between steps.
2. **Cooperative gate** — runs are separate worker processes; before each tool call the
   worker asks a control plane for permission over HTTP.
3. **Push / interrupt** — the control plane pushes state to workers over a live channel
   (websocket, pub/sub) and workers react.
4. **Hard process control** — the control plane owns the worker's process or container
   and freezes it with `SIGSTOP` / container pause / kill.

The decisive consideration is that **the agent most in need of quarantine is the one
least likely to cooperate.** A purely cooperative protocol is unenforceable against a
worker stuck in a loop, wedged, or running modified code. But the layers that do not
need cooperation (3, 4) cannot capture clean state: a paused container tells you nothing
about what the agent was about to do or why, which is exactly what a human inspecting a
frozen run needs.

## Decision

**Runs execute as separate worker processes. A FastAPI control plane owns the state
machine and is the only component permitted to write state transitions. Workers call
`POST /runs/{id}/gate` before every tool call and receive `ALLOW` / `DENY` / `FREEZE`.**

**A heartbeat reaper is the backstop.** Workers heartbeat while they work; a background
task in the control plane escalates any run whose heartbeat goes stale. Silence is
treated as misbehavior rather than ignored.

The worker-side SDK that implements this protocol is **a convenience, not a trust
boundary**. Every safety property must hold when the SDK is bypassed.

## Consequences

**We accept:**

- A network hop on every tool call, and the latency and failure modes that come with it.
- Shared durable storage between control plane and workers; neither can hold state in
  memory alone.
- A lag between a worker going rogue and the reaper noticing, bounded by the heartbeat
  timeout. Freezing is not instantaneous and must not be described as such.
- The control plane becomes a dependency of every run. Its unavailability behaviour is a
  real design question, not an edge case — see the fail-open/fail-closed rule in the spec.

**We get:**

- Real process isolation: a crashing or wedged agent cannot take down the control plane.
- A clean capture point. Because the gate sees the *proposed* tool call before it runs,
  a frozen run preserves the decision the agent was about to make — the single most
  useful artifact for a human deciding whether to release it.
- Runs that survive a control plane restart, since state is durable rather than in-process.
- An append-only event log as a by-product of the protocol, which serves the LLM judge
  and human inspection without being built separately.

**Rejected because:** In-process (1) offers no isolation — the failure we are defending
against takes the safety mechanism down with it. Push (3) requires a live channel per
worker and *still* depends on the worker honouring the message between steps, so it buys
latency at the cost of infrastructure without removing the cooperation assumption. Hard
process control (4) works on an uncooperative agent but destroys the state capture that
makes a freeze useful, and couples us to a specific container runtime. It remains
available as a future escalation beneath `TERMINATED` if the reaper proves insufficient.
