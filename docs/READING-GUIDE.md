# Reading this repository

A route through ~2,000 lines of source, ordered so that each file makes the next one
make sense. Not a directory listing — those you can get from `ls`.

Budget roughly 90 minutes for the full route. The first two stops are 20 of those and
carry most of the value; if you stop after them you will still be able to hold a useful
conversation about this system.

---

## 0. The one-paragraph version

Agents run as separate worker processes. Before every tool call they ask this service
for permission, and it answers `ALLOW`, `DENY` or `FREEZE`. A run sits on one rung of an
ordered ladder — `HEALTHY → DEGRADED → FROZEN → TERMINATED`. Cheap deterministic rules
and an LLM judge move it *up* automatically; only an authenticated human moves it back
*down* from `FROZEN`. A tool-risk registry decides where the judge runs, what `DEGRADED`
still permits, and what happens when a dependency is unreachable.

---

## 1. Why, before what — the ADRs (15 min)

**Read these first.** The code's shape is not recoverable by reading the code. Three
decisions drive everything, and each records the alternatives that were rejected and why:

| Read | Question it answers |
|---|---|
| [`docs/adr/0001-control-plane-with-cooperative-gate.md`](adr/0001-control-plane-with-cooperative-gate.md) | Why ask permission instead of just killing the process? |
| [`docs/adr/0002-graded-quarantine-ladder.md`](adr/0002-graded-quarantine-ladder.md) | Why four states instead of a boolean? |
| [`docs/adr/0003-tier-llm-judge-by-tool-risk.md`](adr/0003-tier-llm-judge-by-tool-risk.md) | Why does the judge run on some calls and not others? |

The through-line: **containment must preserve evidence.** Killing a runaway agent leaves
you with a dead worker and no answer to "what was it about to do?" — so the gate sees
the *proposed* call before it runs, and a frozen agent is frozen holding the action it
was about to take.

If you only read one, read ADR-0002. The ladder is the idea everything else serves.

## 2. What it is for — the PRD (5 min)

[`docs/prd/2026-09-17-agent-quarantine.md`](prd/2026-09-17-agent-quarantine.md) — skim
§1 (problem), §4 (success metrics) and §6 (risks).

§4 contains the claim the whole design stands or falls on: *a frozen run should be
diagnosable from what the control plane captured alone.* If an operator still needs the
worker's logs, containment is not evidential and the premise has failed. Several design
decisions only make sense once you have read that sentence.

§8 records four questions that were open during design and how each was resolved. The
reasoning is kept, not just the answer.

---

## 3. The centre — `quarantine/domain/` (20 min, ~400 lines)

This package performs **no I/O**: no network, no database, no filesystem, no clock
reads. Time arrives as a parameter. That constraint is not stylistic — it is what keeps
the hot path fast and what lets the rules be tested with no fixtures at all.

Read in this order:

1. **`states.py`** — the vocabulary. `RunState` (the ladder), `Decision`, `ToolRisk`,
   `Severity`, `EventKind`. Five enums, and the whole system's nouns.
2. **`models.py`** — immutable value objects. Note `ToolCall` is a *proposal*, and
   `RuleContext` is assembled by the caller so rules never reach for storage.
3. **`registry.py`** — 45 lines, disproportionate importance. One table mapping tool →
   blast radius, with four consumers. An unclassified tool is `HIGH`; that default is a
   correctness property, not a convenience.
4. **`machine.py`** — the transition table, and the **only** writer of `run.state`.
   Notice `escalate()` and `release()` are separate functions with separate
   authorisation. That separation is deliberate and load-bearing (see Traps).
5. **`rules.py`** — six detectors, each a pure function. Read `loop` and `flap` closely;
   they are the two with real subtlety.

By the end of this section you understand the entire decision model. Everything after
is plumbing.

## 4. Where it composes — `quarantine/gate.py` (15 min, 253 lines)

The hot path. Read `Gate.decide()` slowly — **the order of its statements carries the
semantics**, and the docstring explains each ordering choice:

- `TERMINATED` is rejected before anything is written (a caller bug must not leave
  evidence the run was live)
- the `PROPOSED` event is appended *before* the frozen short-circuit (a frozen run must
  still record what it was about to do — this is the point of freezing rather than
  killing)
- the trajectory snapshot is taken *before* that append (otherwise the proposed call is
  double-counted and the loop rule trips a call early)
- rules run *before* the tool-risk lookup (an escalation must stop *this* call, not the
  next one)

Then read the decision matrix in
[`docs/superpowers/specs/2026-09-17-agent-quarantine-design.md`](superpowers/specs/2026-09-17-agent-quarantine-design.md)
§5 and check it against the code. Every `state × tool_risk` cell has a defined answer.

## 5. The edges (25 min)

Now the plumbing, in descending order of interest:

- **`sqlite_store.py`** (313 lines) — persistence. Read `record_state_change` and
  `append_event`: the first wraps a state change and its audit row in one transaction so
  a crash cannot separate them; the second allocates `seq` *inside* the insert because
  allocating it separately raced. Note `save_run` is a targeted `UPDATE` that physically
  cannot carry `state` — the machine-only-writer rule enforced by the storage layer
  rather than by convention.
- **`api.py`** (304 lines) — HTTP. Two audiences: unauthenticated worker endpoints, and
  operator endpoints that require a human. Read the `operator` dependency and note there
  is no fallback to a system actor.
- **`sdk.py`** (170 lines) — what an agent developer imports. **It is not a trust
  boundary** — every safety property must hold when a worker bypasses it, which is what
  the reaper is for. What it *does* own is the client-side fail-closed/fail-open tiering.
- **`judge.py`** (105 lines) — the LLM classifier. Note every failure path becomes
  `JudgeUnavailable`; it never returns a clear verdict to paper over an outage.
- **`reaper.py`** (51 lines) — treats silence as misbehaviour. Two `continue` guards,
  both load-bearing; the file explains each.
- **`main.py`** (44 lines) — production wiring. Refuses to boot without an operator token.

---

## 6. Trace one call end to end (10 min)

This is what makes it click. Follow a single tool call:

1. A worker calls `QuarantineClient.guard(...)` (`sdk.py`)
2. → `POST /runs/{id}/gate` (`api.py`)
3. → `Gate.decide()` (`gate.py`) — snapshot history, record the proposal
4. → `rules.first_firing()` (`domain/rules.py`) — does anything fire?
5. → `registry.risk_of()` — `LOW` returns `ALLOW` here; `HIGH` blocks on the judge
6. → on an adverse verdict, `machine.escalate()` writes one rung and an audit row
7. → back through `guard`, a `DENY` raises `ToolRefused` the agent can work around

Then trace an escalation: run `tests/test_detector_integration.py` and read what it
does. It drives real HTTP calls until a detector trips, which is the clearest picture of
the system actually working.

---

## 7. The invariants

These hold across the whole codebase. A change that breaks one is a defect even if every
test passes — several of these have no test that would notice.

| Invariant | Where |
|---|---|
| `domain/` performs no I/O | `quarantine/domain/` |
| `machine.py` is the only writer of `run.state` | `domain/machine.py`, enforced in `sqlite_store.save_run` |
| `escalate()` and `release()` never share an implementation | `domain/machine.py` |
| Escalation moves exactly one rung | `machine.next_rung` |
| An unclassified tool is `HIGH` | `domain/registry.py` and `sdk.py` |
| Event and transition logs are append-only | no `UPDATE`/`DELETE` exists in `sqlite_store.py` |
| Time is injected | `clock.py` is the only real-clock read |
| The `LOW`-risk gate path does no I/O beyond storage | `gate.py` |

## 8. Traps

Things that look wrong and are not. Each was found the hard way.

- **`api.py` alone lacks `from __future__ import annotations`.** Deliberate. Under
  PEP 563 the `Operator` alias — a closure local — cannot be resolved, FastAPI silently
  drops the `Depends` marker, and every operator route returns 422. There is a comment;
  do not "fix" the inconsistency.
- **`gate.py`'s history limit carries a `4 ×` multiplier.** The window is measured in log
  rows while the rules think in tool calls. Without it, `call_rate` can never fire.
- **`rules.loop` filters to `PROPOSED` *before* slicing the window.** Same root cause.
  Reversing those two lines silently disables the detector at `loop_repeats ≥ 4`.
- **`escalate()` and `release()` look like they want to be one function** with a
  direction parameter. Unifying them is what would let an automatic path un-freeze a run
  it froze.
- **The judge's `except Exception` is broad on purpose.** Every failure must become
  `JudgeUnavailable` so the gate fails closed. A judge bug presenting as an outage is
  much better than one presenting as approval.
- **`/trajectory` calls `get_run()` and discards the result** — that is how it produces a
  404 for an unknown run.

## 9. How much to trust the tests

258 tests, and they are not equal evidence:

- **147 in the seven "contract" files** (`test_registry`, `test_machine`, `test_rules`,
  `test_gate`, `test_failure_modes`, `test_reaper`, `test_api`) were written from the
  spec **before any implementation existed**, and have been byte-identical ever since.
  These are the strongest evidence in the repo.
- **The rest were written alongside the code they test**, so they prove the code agrees
  with itself. Weight accordingly.
- **`tests/conftest.py`'s `InMemoryRepository` is a fake**, and most tests run against
  it. It is known to diverge from the real SQLite store in at least two places (see
  Known gaps). Storage and concurrency tests deliberately use the real store.

A green suite here does not mean "correct" — two detectors passed their unit tests while
being completely dead in the assembled system. `tests/test_detector_integration.py`
exists because of that.

## 10. Known gaps

Stated in the README and worth reading before you trust anything: nothing schedules
`Reaper.sweep()`; background judging only fires when a scheduler is injected; no HTTP
transport ships with the SDK; gate decisions made during an outage are not replayed.

---

## If you have 10 minutes, not 90

Read ADR-0002, then `domain/states.py`, then `Gate.decide()` in `gate.py`. That is the
ladder, the vocabulary, and the decision — the rest is how those three reach a database
and a socket.
