# Control-plane integrity audit

Date: 2026-09-19. Baseline: `aab2c5b67cf62d3fdb06671bc2be339a0ece74d8`.
Scope: repository-wide study, with an implementation limited to decision,
accounting and judge-input integrity. No live model, production database or
worker process was used. This is not a security certification or deployment.

## Architecture and evidence boundary

The tracked tree contains the domain model/machine/rules/registry, gate, HTTP API,
SDK, judge adapter, reaper, SQLite repository, production factory, tests, package
metadata, PRD, three ADRs, approved design, reading guide and implementation plan.
There is no tracked CI workflow, license, security policy or contribution guide
at the baseline. The audit enumerated these from Git rather than assuming a
framework-specific layout.

`domain.machine` constructs immutable transitions without I/O. `Gate` evaluates
rules and the risk-dependent judge. SQLite persists runs/events/transitions.
`api.create_app` supplies worker and operator routes. The SDK is cooperative,
not an execution boundary. `main.build` supplies a two-thread judge executor.
The reaper exists but is not scheduled by the production factory.

The existing cooperative and risk-tiered ADRs remain in force. In particular,
this patch does not introduce an OS sandbox, per-run credentials, a different
offline policy or an automatic CI run.

## Reproduced defects and repairs

### High: obsolete state can overwrite containment

`record_state_change` previously committed any snapshot's target state. While a
judge was evaluating a HEALTHY snapshot, another request could terminate the run;
the old CONCERN result then wrote DEGRADED. A CLEAR result could instead return
ALLOW after termination. Merely re-reading before evaluation cannot fix this.

The repository now compares both state and transition-log revision inside the
write transaction. This also rejects ABA: a run can return to the same state and
timestamp through several valid transitions without becoming the same snapshot.
The final decision is checked against current containment in the same transaction
as its event. The lock never spans a model call. Stale background escalation is
discarded and recorded, rather than silently reviving the run.

Controls: `test_a_stale_escalation_cannot_revive_a_terminated_run`,
`test_a_stale_snapshot_is_rejected_even_after_a_same_time_state_cycle`, and
`test_a_judge_finishing_after_termination_never_authorizes_or_revives`.
The concurrency review additionally covers operator conflicts, FROZEN ABA,
decision-time containment and the database write-lock boundary.

### High: usage can be lost, reduced or separated from its evidence

The outcome handler read a snapshot and wrote absolute counters. Two 7-token
outcomes could leave 7 rather than 14 tokens; heartbeat writes could restore old
counters too. A failure appending the audit event left usage committed without
its outcome row. Negative usage was accepted with HTTP 200 and reduced counters.
Oversized integers/deadlines escaped into driver/datetime exceptions.

Outcomes now apply deltas, errors, liveness and the event in one transaction.
Heartbeats update liveness only. Usage/budgets require actual non-negative int64
JSON integers, deadlines require a representable positive interval, and accumulated
usage overflow refuses before mutation. Zero budgets/usage remain supported.

Controls: `test_concurrent_outcomes_preserve_both_increments`,
`test_failed_outcome_audit_insert_rolls_back_counters`,
`test_heartbeat_cannot_overwrite_concurrent_usage`, invalid-input cases and
`test_zero_usage_and_zero_budget_are_valid`.

### High: a reaper can freeze a worker that just reported liveness

State revision alone is insufficient: heartbeat/outcome writes intentionally do
not change containment. A stale reaper selection could therefore pass the state
check even after the worker resumed. The first draft exhibited eight failing
interleavings across HEALTHY/DEGRADED, heartbeat/outcome and selection/commit.

The reaper now supplies its selected heartbeat as an additional transactional
precondition. Fresh liveness wins; a genuinely silent run still escalates. The
review tests include both cases and old-schema database compatibility.

### Medium: log traffic changes background judging frequency

Sampling on `next_seq % J` counted PROPOSED, DECISION, OUTCOME and JUDGE rows as
though they were tool calls. With J=3 and normal outcome reports, no background
judging occurred in the controlled 20-call run. Other patterns shifted the phase
or over-sampled. HIGH calls could be judged both synchronously and asynchronously.

Sampling now uses the allocated proposal sequence to count LOW proposals only.
Tests assert exact trigger ordinals, not merely total jobs or gaps, for J=3/10,
with/without outcomes, and drained/queued callbacks. Interleaved HIGH calls do
not shift the LOW count. The protocol returns the actual allocated append
sequence so concurrent callers cannot accidentally share a later ordinal.

Controls: all cases in `test_background_cadence.py`.

### Medium: judge input loses argument identity and retries hide latency

The SDK could not supply the preview already accepted by HTTP, and the judge
prompt omitted `args_digest`. Distinct argument digests could produce identical
prompts. Optional keyword-only previews now pass through both `gate` and `guard`;
the renderer includes the digest. Existing positional calls and refusal behavior
are preserved. Preview redaction remains the caller's responsibility.

The Anthropic adapter now explicitly disables SDK retries. Transport timeouts
do not imply a strict total deadline; this distinction is documented rather than
claiming the model always returns within ten wall-clock seconds. See the
[official SDK retry/timeout contract](https://platform.claude.com/docs/en/cli-sdks-libraries/sdks/python).

Controls: `test_judge_sdk_regressions.py`, including old-signature and outage
positive controls. These use a fake model transport, not paid model calls.

## Verification

Environment: Python 3.12.3, pytest 9.1.1; repository-local virtual environment.
The initial untouched baseline suite passed 258 tests. Original contract
assertions were not weakened; repository fakes and the broken-store fixture were
updated for the new persistence operations.

The 45 new API/cadence/SDK tests were copied unchanged into a `git archive` of the
baseline and run with the archive explicitly on `PYTHONPATH`: **39 failed, 6
passed**, exit 1. The passing cases are supporting controls, not proof of a
regression. A further 21 concurrency-review tests exercise the implemented
repository contract, including an actual old-format SQLite database.
The subsequent full patch run passed all 324 tests in 58.43 seconds. Three more
boundary cases were then added for cumulative usage overflow and a datetime
overflow whose input still fits int64; these do not replace the earlier evidence.
That 327-case run finished with **326 passed and one failure** in the existing
concurrent-append test (eight locked appends). All 69 added cases passed. The
final result is therefore not an unconditional whole-suite green claim.

The final integration adds eight independently prepared test files and retains
the three late overflow boundary cases rather than copying an older snapshot.
Collection by exact node ID gives **513 unique cases: all 258 baseline IDs plus
255 additions**, with no missing original IDs. The additions are the 69 integrity
cases and 186 broader contract/integration cases. The combined run finished with
**512 passed, 1 strict xfailed, exit 0 in 68.11 seconds**. The expected failure is
the existing body-less terminate request returning 422; it is not a SQLite waiver.
That run's concurrent-append case passed, but the earlier failures below remain
unresolved. This submission stays a **draft pending contention follow-up**.

Coverage 7.16.1 / pytest-cov 7.1.0 measured production code only: **804/811
statements (99.14%) and 122/126 branches (96.83%)**, combined 98.83%.
These are execution coverage, not proof of model accuracy or operational safety.
The only warning was Starlette's deprecated AnyIO BlockingPortal alias.

Reproduction from the patch checkout:

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -o addopts='' --tb=short
```

The final integration removes three pre-existing unused imports in
`test_gate.py` and `test_failure_modes.py`, without altering assertions.
**Whole-tree `ruff check .`, `git diff --check` and `pip check` pass.**
For the coverage run, install `pytest-cov` into the local virtual environment and
add `-p pytest_cov --cov=quarantine --cov-branch` to the isolated pytest invocation.

Independent review reproduced the exact LOW trigger set `{10,20,30,40}` with
and without outcome/judge traffic, discarded an evaluation-time stale background
verdict after a genuine two-step freeze, and preserved 400 tokens / 40 calls
under 40 concurrent HTTP reports. The background-discard case is also retained
against both repository implementations. Review does not certify a contention
remedy: the same reviewer observed five failures in five append-load repetitions.

One full patch run failed the existing
`TestConcurrentEventAppends.test_concurrent_appends_all_land`: two of 200
appends returned `StoreUnavailable('database is locked')`. This is a real
availability limit, not a sequence-key collision, and remains open. No retries,
larger timeout or weaker assertions were added to make the run green.
SQLite permits one writer; `BEGIN IMMEDIATE` can still return BUSY. Its purpose
here is atomic precondition checking, not unlimited write throughput. See the
[SQLite transaction contract](https://www.sqlite.org/lang_transaction.html).
The untouched baseline's seven storage-concurrency tests also produced one
failure at the same node (six locked appends, six tests passed), independently
establishing that the contention failure predates this patch. The later passing
full run does not discharge that observation.

## Compatibility and limits of the repair

- Existing SQLite tables remain usable. Two additive indexes support revision
  lookup and sampling; revision comes from existing append-only transition IDs.
- Custom Repository implementations must implement the new atomic outcome,
  heartbeat, decision and proposal-count methods, return allocated append
  sequences, and enforce state revision plus optional heartbeat preconditions.
- `save_run` remains for compatibility, but is explicitly unsafe for concurrent
  counter updates. No HTTP handler calls it. Raw database writes or a custom
  repository that ignores the protocol can still violate invariants.
- `409` on an operator/complete conflict requires a fresh read. There is no
  automatic replay of a stale operator decision.
- An ALLOW is linearized with its event. A later operator action cannot revoke
  bytes already returned to a worker. Future usage is not reserved, and outcome
  retries have no idempotency key; neither property is claimed by this change.
- Tests control scheduling at protocol boundaries but execute real SQL/HTTP
  paths. They do not prove fairness, a performance SLO or model accuracy.

## Remaining work, prioritized separately

| Priority | Gap | Evidence and next decision |
|---|---|---|
| High | Production liveness sweep is not scheduled | `main.build` creates no reaper loop. Add lifecycle-owned scheduling/shutdown and a clock-driven integration test. This is already disclosed upstream. |
| High | Cooperative boundary can be bypassed | SDK and HTTP cannot suspend a hostile process. Trusted executor/OS enforcement needs a separate architecture and failure/recovery contract, not an Enforcer callback inside the pure state machine. |
| High | Worker endpoints have no per-run authentication | A caller knowing a run ID can submit telemetry/complete. Scope credentials and rotation before exposure outside a trusted single-operator network. |
| Medium | SQLite contention remains observable | Repeated concurrent writers can exceed the driver wait. Measure latency/throughput and design bounded backpressure; do not declare success from one passing run. |
| Medium | Background work has no bounded durable queue | `executor.submit` futures are discarded, executor shutdown is not lifecycle-owned, queued jobs can outlive containment, and callback failures lack a surfaced result. |
| Medium | Judge cannot establish original intent | Registration has no task/allowed-goal context. A digest identifies arguments but does not explain meaning; define a minimal, untrusted, redacted context contract. |
| Medium | Preview and event payloads are unbounded | Event count limits do not bound bytes. Add explicit size/field policy before sending sensitive inputs to a remote model. Prompt injection is not solved by this patch. |
| Medium | Settings validation is incomplete | Environment J/K checks exist, but direct Settings construction and several other thresholds admit invalid values. Test finite/positive constraints without breaking intentional zero-budget behavior. |
| Medium | Detection history is a log-row heuristic | `_history_limit` multiplies call rate by four, not an exact proposal window. Extra events can crowd out relevant history. Query the actual event population/time interval. |
| Medium | Outcome delivery is not idempotent | No call ID ties proposals/outcomes or deduplicates retry. Define reservation/idempotency separately from atomic addition. |
| Low | Inspection truncates at 1000 events | API exposes recent history without pagination/coverage metadata. A full trajectory cannot be inferred from this endpoint. |
| Low | Body-less terminate is rejected | `POST /runs/{id}/terminate` needs a JSON object even with a default reason; unlike release, an omitted body returns 422. A strict expected-failure test records the discrepancy. |
| Low | SDK needs a caller-provided HTTP transport | A tested transport with lifecycle management would reduce integration errors; offline replay remains an explicit upstream non-goal. |
| Low | Release/maintenance surfaces are incomplete | No baseline CI, supported-version matrix, lock policy, license, security contact or contribution guide. Maintainers must choose these; the audit does not invent a license or enable CI. |

These are not all defects in the accepted scope: several are explicitly deferred
features or security assumptions. They need separate review and must not be
reported as implemented merely because this audit names them.

The repository's pre-existing `CLAUDE.md` still contains an older 253-test green
snapshot. It was not changed as part of this patch; use this dated audit and the
updated README/reading guide for current counts and limitations.
