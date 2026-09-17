# PRD: Agent Quarantine

- **Date:** 2026-09-17
- **Status:** Draft — design phase
- **Tracker item:** _not yet created_
- **Related:** [ADR-0001](../adr/0001-control-plane-with-cooperative-gate.md),
  [ADR-0002](../adr/0002-graded-quarantine-ladder.md),
  [ADR-0003](../adr/0003-tier-llm-judge-by-tool-risk.md)

---

## 1. Problem

We run LLM agents that take real actions through tools. When an agent misbehaves — burns
its budget in a loop, drifts onto a task nobody gave it, or reaches for something
destructive — there is currently no way to stop it that does not also destroy the evidence
of what it was doing.

The two options available today are both bad. Let it finish, and it keeps acting. Kill the
process, and you are left with a dead worker and no answer to the question that matters:
*what was it about to do, and why?* Without that answer the same failure recurs, because
nothing was learned from it.

The gap is a containment mechanism that is **proportionate** (not every problem deserves a
kill), **evidential** (containment preserves the trajectory), and **not dependent on the
misbehaving agent's cooperation** to work.

## 2. Users

| User | Needs |
|---|---|
| **Agent operator** (us, on call) | To find out an agent went wrong without reading logs; to see what it was about to do; to stop it or declaw it; to release it once understood. |
| **Agent developer** (us, building agents) | A safety net that lets them ship an agent without hand-rolling budget checks and loop detection into every one. Clear feedback when their agent gets contained, and why. |
| **The agent itself** | To be told a tool is refused in a way it can respond to, rather than failing opaquely. |

There is no external or customer-facing user. This is internal infrastructure.

## 3. Solution

A **control plane** that agents check in with before they act.

Every agent run registers with the service. Before each tool call, the worker asks whether
it may proceed and reports what it has done since last time. The service evaluates that
against cheap deterministic rules and — for anything with real blast radius — an LLM judge
that reads the run's recent trajectory. It answers allow, deny, or freeze.

Containment is **graded**, not binary. A run moves along a ladder:

| State | Meaning |
|---|---|
| `HEALTHY` | Normal operation. |
| `DEGRADED` | Still working, but stripped to safe tools. Destructive calls are refused. |
| `FROZEN` | Suspended. The trajectory is preserved for a human to inspect. |
| `TERMINATED` | Over. Cannot resume. |

The point of the ladder is that an uncertain signal gets a proportionate response.
A detector that is only sometimes right can safely move a run to `DEGRADED`, because being
wrong costs some capability rather than the whole run.

An agent that stops checking in is escalated anyway: silence is treated as misbehaviour,
not as health. Operators get an inspection and release surface over the same state.

**What this is not:** an agent framework. It holds no prompts, runs no models on the
agent's behalf, and never sees the agent's reasoning except as events the worker reports.
It is a policy decision point.

## 4. Success metrics

| Metric | Target |
|---|---|
| Time from misbehaviour to containment | Rules: the next tool call. Unresponsive worker: within the heartbeat timeout. |
| Gate latency, low-risk tools | Fast enough that developers do not route around it. |
| Frozen runs that a human can diagnose from the captured trajectory alone | The great majority — if operators still need the worker's logs, containment is not evidential and the PRD's core claim has failed. |
| Agents onboarded without hand-rolled budget/loop checks | All of ours. |
| False containment of healthy runs | Rare enough that operators do not learn to ignore or disable it. |

The last one is the real measure. A safety mechanism that people disable has negative value,
because it looks like protection while providing none.

## 5. Scope

**In scope**

- Run registration, the state ladder, and the gate protocol.
- Deterministic rule detectors over run counters.
- An LLM judge over run trajectories, tiered by tool risk.
- A version-controlled tool risk registry.
- Heartbeat monitoring and automatic escalation of silent runs.
- An append-only event log per run; operator inspection and release endpoints.
- A worker-side client library implementing the protocol.

**Out of scope**

- Running or orchestrating agents. Something else starts them; we contain them.
- Prompt or model-level safety. This layer sees actions, not reasoning.
- Multi-tenancy, HA, and horizontal scale — single-box internal tool.
- Sandboxing the worker's process or filesystem. We gate tool calls; we do not confine
  the process. Noted as a known limit, not a future promise.

## 6. Risks

| Risk | Why it matters | Mitigation |
|---|---|---|
| **The protocol is cooperative** | A worker running modified or wedged code can skip the gate entirely. | Heartbeat reaper escalates silence. Documented honestly as a limit — this is not a security boundary against a hostile agent, it is a safety mechanism against a broken one. |
| **The control plane is a dependency of every run** | An outage stalls or unguards every agent. | Fail-open / fail-closed decided per tool risk level, not globally. **Open question — see §8.** |
| **The judge is wrong** | False positives contain healthy agents; false negatives miss real harm. | The ladder makes false positives cheap. False negatives remain a real residual risk. |
| **Judge cost and latency** | Per-call model spend on a hot path. | Tiered by blast radius — most calls never invoke it. |
| **Unclassified tools** | A tool missing from the registry silently bypasses the judge. | Missing entry is treated as `HIGH` and fails loudly in tests. |
| **The mechanism gets disabled** | The classic end state of a noisy safety tool. | Track false containments as a first-class metric; make `DEGRADED` the default response to weak signals. |

## 7. Epic & user stories

**Epic: Agent Quarantine — proportionate, evidential containment for misbehaving agents.**

Each story below becomes a tracker item. Acceptance criteria are the contract.

### Story 1 — Register a run and gate its tool calls
As an agent developer, I want my worker to register a run and ask permission before each
tool call, so containment decisions can reach it.
- A worker can register a run and receives an id.
- `POST /runs/{id}/gate` accepts a proposed tool call and returns `ALLOW`, `DENY` or `FREEZE`.
- Each gate call appends an event recording the proposal and the decision.
- A gate call against an unknown or terminated run is rejected, not silently allowed.

### Story 2 — Move a run along the quarantine ladder
As an operator, I want a run to occupy exactly one well-defined state, so its containment
is unambiguous.
- States are `HEALTHY`, `DEGRADED`, `FROZEN`, `TERMINATED`.
- Only the state machine writes transitions; every transition records cause and actor.
- Escalation moves one step; forbidden transitions are rejected.
- Gate decisions follow from state: `DEGRADED` denies `HIGH`-risk tools, `FROZEN` freezes all.

### Story 3 — Escalate on deterministic rules
As an operator, I want countable misbehaviour to contain a run automatically.
- Rules cover at minimum: budget exceeded, tool-call loop, error streak, wall-clock limit.
- Each rule is a pure function over run state and is independently testable.
- A firing rule escalates one step and records which rule fired.
- A `DEGRADED` run auto-recovers to `HEALTHY` after a configured clean interval.
- Repeated degradation within a window escalates to `FROZEN` instead of recovering.

### Story 4 — Classify trajectories with an LLM judge
As an operator, I want semantic misbehaviour caught that rules cannot express.
- The judge reads a run's recent trajectory and returns a verdict with a reason.
- `HIGH`-risk tool calls block on the judge; `LOW`-risk trajectories are judged in the background.
- Judge failure follows the documented fail-open/fail-closed rule and is never silent.

### Story 5 — Classify tools by blast radius
As an agent developer, I want each tool's risk declared in one reviewable place.
- Risk levels live in version-controlled config, not the database.
- An unclassified tool is treated as `HIGH` and fails loudly.
- The registry drives all three of: judge placement, `DEGRADED` permissions, failure behaviour.

### Story 6 — Escalate runs that stop checking in
As an operator, I want a silent worker contained without its cooperation.
- Workers heartbeat while working; the timeout is configurable.
- A background reaper escalates stale runs and records the cause as a timeout.
- A worker that resumes checking in after being escalated is not silently restored.

### Story 7 — Inspect and release a contained run
As an operator, I want to see why a run was contained and decide what happens next.
- List runs by state; fetch one run's full trajectory and transition history.
- The record shows the tool call the run was about to make when it was frozen.
- Releasing a `FROZEN` run requires an authenticated human; the action records who did it.
- The release endpoint takes a target state and defaults to `DEGRADED`.
- A `DEGRADED` run auto-recovers without human action; a `FROZEN` run never does.

### Story 8 — Adopt quarantine from a worker
As an agent developer, I want to add quarantine to an existing agent without rewriting it.
- A client library wraps tool execution: gate before, report after, heartbeat throughout.
- A `DENY` surfaces to the agent as a refusal it can react to, not an opaque failure.
- Documented so an agent can be onboarded from the README alone.

## 8. Open questions

_All resolved as of 2026-09-17. Retained with their reasoning; resolutions are load-bearing._

1. ~~**Who may de-escalate a run?**~~ **Resolved 2026-09-17:** `FROZEN` requires an
   authenticated human; `DEGRADED` auto-recovers after a clean interval. Repeated
   degradation within a window escalates to `FROZEN` rather than recovering, so that
   intermittent misbehaviour cannot hide behind automatic recovery. See
   [ADR-0002](../adr/0002-graded-quarantine-ladder.md#de-escalation-decided-2026-09-17).
   Adds a flap-detection requirement to Story 3.
2. ~~**Fail-open or fail-closed when the control plane is unreachable?**~~ **Resolved
   2026-09-17:** tiered by tool risk. `HIGH`-risk calls fail **closed**; `LOW`-risk calls
   fail **open**. An outage therefore degrades every agent to safe mode rather than
   stopping them or leaving them unguarded — the same proportionate response the ladder
   applies to uncertain detectors. Applies identically to an unreachable judge and to
   unavailable storage. Failures are always logged as events, never swallowed.
3. ~~**Does a released run resume into `HEALTHY` or `DEGRADED`?**~~ **Resolved
   2026-09-17:** the releasing human chooses; the release endpoint takes a target state and
   **defaults to `DEGRADED`**. The person releasing has the context to judge, and the safe
   default is the one that keeps the run declawed until it has proven itself.
4. ~~**Is the judge's verdict advisory or binding?**~~ **Resolved 2026-09-17:**
   **binding**. A judge verdict escalates the run one rung exactly as a rule firing does —
   one escalation path, one code path. The ladder is what makes a wrong verdict affordable:
   the cost of a hallucinating judge is a `DEGRADED` run, not a dead one.
