# Spec: Agent Quarantine — control plane

- **Date:** 2026-09-17
- **Status:** Approved
- **Tracker item:** _tracker not yet chosen; see `CLAUDE.md`_
- **PRD:** [docs/prd/2026-09-17-agent-quarantine.md](../../prd/2026-09-17-agent-quarantine.md)
- **ADRs relied on:** [ADR-0001](../../adr/0001-control-plane-with-cooperative-gate.md),
  [ADR-0002](../../adr/0002-graded-quarantine-ladder.md),
  [ADR-0003](../../adr/0003-tier-llm-judge-by-tool-risk.md)

---

## 1. Overview

A FastAPI control plane that contains misbehaving LLM agents. Agent runs execute as
separate worker processes and call the gate before each tool call; the control plane
answers `ALLOW`, `DENY` or `FREEZE`.

This spec defines the contract. The reasoning behind the shape lives in the ADRs and is
not repeated here.

## 2. Components

| Module | Responsibility | Depends on |
|---|---|---|
| `domain.states` | `RunState`, `Decision`, `Severity`, `ToolRisk` enums; ladder ordering | — |
| `domain.models` | `Run`, `Event`, `Transition`, `ToolCall`, `Verdict` | `states` |
| `domain.registry` | tool name → `ToolRisk`; unknown resolves to `HIGH` | `states` |
| `domain.rules` | six pure detectors over `Run` + proposed call | `models`, `config` |
| `domain.machine` | the only writer of transitions | `models`, `store` |
| `gate` | the decision algorithm (§5) | all of the above, `judge` |
| `judge` | `Judge` protocol; Claude-backed implementation | `models` |
| `reaper` | escalates runs with stale heartbeats | `machine`, `store`, `clock` |
| `store` | `Repository` protocol | `models` |
| `sqlite_store` | SQLite (WAL) implementation | `store` |
| `api` | FastAPI routes (§6) | `gate`, `machine`, `store` |
| `clock` | `Clock` protocol; injected everywhere time is read | — |

`domain.*` performs no I/O. That is what keeps the `LOW`-risk gate path fast and the
rules trivially testable.

## 3. Data model

**`runs`** — `id` (uuid), `agent_name`, `state`, `state_since`, `created_at`,
`last_heartbeat_at`, `tokens_used`, `cost_cents`, `tool_calls`, `consecutive_errors`,
`budget_tokens`, `budget_cost_cents`, `deadline_at`.

**`events`** — `id`, `run_id`, `seq` (monotonic per run), `kind`, `tool_name`,
`args_digest`, `payload` (JSON), `created_at`. Append-only. Kinds: `PROPOSED`, `DECISION`,
`OUTCOME`, `JUDGE`, `SYSTEM`.

**`transitions`** — `id`, `run_id`, `from_state`, `to_state`, `cause`, `actor`, `detail`,
`created_at`. Append-only. `actor` is `"system"` or `"human:<id>"`.

The tool risk registry is **configuration**, not a table (ADR-0003).

## 4. State machine

Ladder order: `HEALTHY < DEGRADED < FROZEN < TERMINATED`.

**Permitted transitions. Anything absent is rejected with an error.**

| From | To | Operation | Actor |
|---|---|---|---|
| `HEALTHY` | `DEGRADED` | `escalate` | system |
| `DEGRADED` | `FROZEN` | `escalate` | system |
| `FROZEN` | `TERMINATED` | `terminate` | human |
| `HEALTHY`, `DEGRADED` | `TERMINATED` | `complete` (run finished) | system |
| `HEALTHY`, `DEGRADED`, `FROZEN` | `TERMINATED` | `terminate` | human |
| `DEGRADED` | `HEALTHY` | `auto_recover` | system |
| `FROZEN` | `DEGRADED` or `HEALTHY` | `release` | human |

**Invariants:**

1. `TERMINATED` has no outgoing edges.
2. `escalate` moves exactly one rung. An immediate freeze from `HEALTHY` is two recorded
   transitions, never one.
3. `escalate` and `release` are separate functions with separate authorisation. They must
   not be unified into a signed-delta helper.
4. Every transition writes a `transitions` row with cause and actor. No code outside
   `domain.machine` assigns `run.state`.
5. `release` from `FROZEN` requires a human actor and is rejected without one.
6. `auto_recover` applies only to `DEGRADED`.

## 5. The gate algorithm

`POST /runs/{id}/gate` with a proposed `ToolCall`.

```
run = store.get(run_id)                      # missing        -> 404
if run.state is TERMINATED:                  #                -> 409
append event PROPOSED

if run.state is FROZEN:
    return FREEZE

fired = first rule in RULES that fires for (run, proposed_call)
if fired:
    machine.escalate(run, cause=fired.name, actor="system")
elif run.state is DEGRADED and clock.now() - run.state_since >= RECOVERY_INTERVAL:
    machine.auto_recover(run)

if run.state is FROZEN:
    return FREEZE

risk = registry.risk_of(proposed_call.tool)  # unknown tool   -> HIGH

if run.state is DEGRADED and risk is HIGH:
    return DENY

if risk is HIGH:
    try:
        verdict = judge.evaluate(run, trajectory)
    except JudgeUnavailable:
        append event JUDGE (unavailable)
        return DENY                          # fail closed (ADR-0003)
    if verdict.severity >= CONCERN:
        machine.escalate(run, cause="judge", actor="system")
        return DENY

return ALLOW                                 # LOW-risk judging happens out of band
```

**Why that last `DENY` is unconditional.** The judge is reachable only from `HEALTHY`:
a `FROZEN` run returns at step 2 and a `DEGRADED` one returns at step 5. So an adverse
verdict always lands on `HEALTHY → DEGRADED`, and the call is refused because it is
`HIGH`-risk. A `FREEZE` on this path would be unreachable code, and unreachable code in
the gate is a place for a wrong assumption to hide.

**Decision matrix** — every combination is defined:

| State | `LOW` tool | `HIGH` tool |
|---|---|---|
| `HEALTHY` | `ALLOW` | judge; `ALLOW` if clear, else escalate → `DENY` |
| `DEGRADED` | `ALLOW` | `DENY` (judge not consulted — the answer cannot change) |
| `FROZEN` | `FREEZE` | `FREEZE` |
| `TERMINATED` | error | error |

Unclassified tools take the `HIGH` column. Every gate call appends a `DECISION` event
regardless of outcome.

## 6. HTTP surface

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/runs` | Register a run; returns its id |
| `POST` | `/runs/{id}/gate` | The gate (§5) |
| `POST` | `/runs/{id}/outcome` | Report a completed tool call; updates counters |
| `POST` | `/runs/{id}/heartbeat` | Liveness |
| `POST` | `/runs/{id}/complete` | Worker finished normally → `TERMINATED` |
| `GET` | `/runs` | List, filterable by state |
| `GET` | `/runs/{id}` | Run detail with transition history |
| `GET` | `/runs/{id}/trajectory` | The event log |
| `POST` | `/runs/{id}/release` | Human release; body carries target state, default `DEGRADED` |
| `POST` | `/runs/{id}/terminate` | Human termination |

Operator endpoints (`release`, `terminate`, and the inspection reads) require an
authenticated human actor; the worker protocol endpoints do not. Credentials are two
headers: `X-Operator-Token`, checked against `settings.operator_token`, and
`X-Operator-Id`, which is recorded on the transition as `human:<id>`. A missing or wrong
token is rejected with `401`/`403` and never falls back to a system actor — a worker must
not be able to release itself.

A shared secret is sufficient for a single-box internal tool, and is the reason
multi-tenancy is out of scope in the PRD.

## 7. Rules

Each is `(ctx: RuleContext, settings: Settings) -> RuleVerdict | None`, pure, no I/O.
`RuleContext` carries the run, the proposed call, recent events, recent transitions and the
current time — assembled by the caller so that rules never reach for storage themselves.
Rules are evaluated in `RULES` order; the first to fire wins and is recorded as the cause,
which keeps the recorded cause deterministic when several fire at once.

The proposed call is part of the context, so `loop` counts the call that is *about* to
repeat: the third identical request is refused before it runs rather than after.

| Rule | Fires when | Default threshold |
|---|---|---|
| `budget` | `tokens_used > budget_tokens` or `cost_cents > budget_cost_cents` | per-run, required at registration |
| `loop` | ≥ N events with the same `(tool_name, args_digest)` in the last M events | N=3, M=10 |
| `error_streak` | `consecutive_errors >= N` | N=5 |
| `wall_clock` | `clock.now() > deadline_at` | per-run |
| `call_rate` | tool calls in the trailing minute > N | N=60 |
| `flap` | ≥ N `HEALTHY→DEGRADED` transitions within window W | N=3, W=1h |

`flap` reads transition history rather than run counters. It needs no special handling in
the machine: firing while a run is `DEGRADED` escalates it one rung to `FROZEN`, which is
exactly the required behaviour, and it simultaneously suppresses auto-recovery because a
rule fired this pass.

Thresholds live in `config` with the defaults above; budgets and deadlines are per-run and
supplied at registration.

## 8. Judge

```python
class Judge(Protocol):
    def evaluate(self, run: Run, trajectory: Sequence[Event]) -> Verdict: ...
```

`Verdict` carries `severity` (`CLEAR` < `CONCERN` < `SEVERE`) and a `reason` string shown
to operators on the inspection page. It reads the last `K=20` events (configurable).

- `HIGH`-risk calls block on it, with a timeout; exceeding the timeout raises
  `JudgeUnavailable`.
- `LOW`-risk trajectories are judged out of band every `J=10` gate calls; the verdict
  applies from the next gate call.
- A `CONCERN` or `SEVERE` verdict escalates one rung — binding, identical to a rule firing.
- Every invocation appends a `JUDGE` event, including failures. Judge errors are never
  swallowed.
- No unit test calls a real model. The production implementation targets the Claude API;
  tests use a fake behind the protocol.

## 9. Failure handling

| Failure | Behaviour |
|---|---|
| Gate call on unknown run | `404`. Never a permissive default. |
| Gate call on `TERMINATED` run | `409`. Never `FREEZE` — that would hide a caller bug. |
| Worker silent past heartbeat timeout | Reaper escalates **one rung per pass**, cause `heartbeat_timeout`. Two passes take a live-looking run to `FROZEN`. |
| Escalated worker resumes calling | Receives the current state's decision. Liveness is not evidence of health; it does **not** restore. |
| Judge timeout or error | `HIGH`: `DENY` (fail closed). `LOW`: logged, background judging skipped. |
| Storage unavailable | Control plane cannot decide; returns `503`. Workers apply the same tiering client-side: `HIGH` blocked, `LOW` proceeds. |
| Control plane unreachable from worker | SDK fails **closed** for `HIGH`-risk tools, **open** for `LOW`. Every such instance is reported as an event once connectivity returns. |

Net effect of the tiering: an outage degrades every agent to safe mode rather than halting
them or leaving them unguarded.

## 10. Configuration

`config.py`, environment-overridable: rule thresholds (§7), `RECOVERY_INTERVAL` (default
15 min), `HEARTBEAT_TIMEOUT` (default 2 min), judge timeout (default 10 s), `K` and `J`
(§8), the shared-secret operator token, and the database path.

**The tool risk registry is a version-controlled Python mapping**, not environment
configuration — it is code-reviewed. A tool absent from it resolves to `HIGH`, and a
startup check logs every unclassified tool seen at runtime.

## 11. Testing

| Layer | Approach |
|---|---|
| Rules | Table-driven over constructed `Run` values. No fixtures, no clock, no DB. |
| State machine | Exhaustive over `from × to`, asserting rejection of every non-permitted pair, plus the six invariants of §4. |
| Gate | The §5 decision matrix enumerated, including `FROZEN` + `LOW` and `DEGRADED` + unclassified. |
| Registry | Every configured tool is classified; an undeclared tool resolves to `HIGH`. |
| Reaper | Injected clock — no `sleep` in tests. |
| Failure modes | Fake judge raising `JudgeUnavailable`; store raising; assert the §9 tiering. |
| API | FastAPI `TestClient` against an in-memory repository. |

Time and the judge are injected everywhere. No unit test sleeps or makes a network call.

## 12. Out of scope

Running or orchestrating agents; prompt-level safety; multi-tenancy and HA; process or
filesystem sandboxing of workers. The cooperative protocol is a safety mechanism against a
broken agent, not a security boundary against a hostile one.
