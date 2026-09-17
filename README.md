# Agent Quarantine

**A kill switch for LLM agents that doesn't destroy the evidence.**

> **Status: contract suite green; runnable.** The design is settled and all 182 tests
> pass — the domain, the gate, the judge tiering, the reaper, the HTTP surface, persistence
> (`SqliteRepository`, `Settings.from_env`), and the worker SDK are all implemented against
> the spec-first contract. See [Project status](#project-status) and
> [Using it from an agent](#using-it-from-an-agent) to onboard a worker.

---

## The problem, in one paragraph

You run an LLM agent. It has tools. One day it goes wrong — stuck in a loop burning
tokens, wandering onto a task nobody asked for, or reaching for something destructive.
Right now you have two choices: let it finish, or kill the process. Letting it finish
means it keeps acting. Killing it leaves you with a dead worker and no answer to the only
question that matters: **what was it about to do, and why?** Without that, the same thing
happens again next week.

## The idea

Agents check in before they act. A separate service — the **control plane** — decides
whether each tool call may proceed.

```
   ┌─────────────────┐                      ┌──────────────────────┐
   │  Agent worker   │   "may I run         │    Control plane     │
   │  (your agent,   │    send_email?"      │      (FastAPI)       │
   │   own process)  │ ───────────────────► │                      │
   │                 │                      │  • rules             │
   │                 │ ◄─────────────────── │  • LLM judge         │
   └─────────────────┘   ALLOW / DENY /     │  • state machine     │
            │             FREEZE            │  • event log         │
            │                               └──────────────────────┘
            └── heartbeat ──────────────────────────► (silence = trouble)
```

Two things make this different from a boolean "is_blocked" flag:

**1. Containment is graded.** A run sits on a ladder, and weak evidence gets a
proportionate response instead of a death sentence:

| State | What it means for the agent |
|---|---|
| `HEALTHY` | Carry on. All tools available. |
| `DEGRADED` | Keep working, but you've been declawed — read-only tools only. Anything destructive is refused. Recovers on its own after a quiet interval. |
| `FROZEN` | Stop. Your trajectory is preserved so a human can look at it. Only a human can let you out. |
| `TERMINATED` | Over. No resuming. |

This matters because the detectors aren't certain. A rule that fires on a proxy signal, or
a model's opinion about intent, can be wrong. If the only response available is "kill it",
you end up tuning the thresholds so loose that nothing ever fires — and then you have a
safety mechanism that is purely decorative. `DEGRADED` is cheap enough to be wrong about.

**2. Freezing captures the decision, not the corpse.** Because the gate sees the tool call
*before* it runs, a frozen agent is frozen holding the thing it was about to do. That's the
artifact you actually need when deciding whether to release it.

## How a tool call gets decided

Every tool is declared in a registry with a risk level, and that one declaration drives
three separate behaviours:

| Tool risk | Judge | Allowed in `DEGRADED`? | If the control plane is down |
|---|---|---|---|
| `LOW` — read-only, cheap, reversible | Runs in the background; never blocks the call | Yes | Fails **open** — the call proceeds |
| `HIGH` — writes, sends, payments, deletes | Blocks the call until it answers | No | Fails **closed** — the call is refused |

The reasoning: an LLM judge costs seconds and real money, so you don't want it on every
call. But "being a few seconds late" is harmless for a search and unacceptable for a
payment. The cost of being late is a property of the *tool*, not of the detector — so the
tool decides where the judge runs.

That last column means **an outage degrades every agent to safe mode** rather than stopping
them dead or leaving them unwatched — the same proportionate response the ladder applies to
uncertain detectors.

**A tool that isn't in the registry is treated as `HIGH` and fails loudly.** Otherwise the
registry quietly rots into a permit-list for anything new.

## Using it from an agent

The worker SDK (`quarantine.sdk.QuarantineClient`) is the thing an agent developer
actually imports. It wraps the HTTP protocol above — gate before, report after,
heartbeat throughout — so a worker doesn't hand-roll it. It is **not** a trust
boundary: nothing in it is relied on for a safety property, and every guarantee
above holds even if a worker skips it entirely. What it must get right is the
client-side half of the fail-open/fail-closed tiering, because that's the
behaviour that applies exactly when it can't reach the control plane at all.

**No HTTP transport ships with the SDK.** `QuarantineClient` takes any object with
a `post(path: str, body: dict) -> dict` method that raises `ConnectionError` when
the control plane is unreachable — you supply it. A minimal one over `httpx`:

```python
import httpx

class HttpTransport:
    def __init__(self, base_url: str):
        self._client = httpx.Client(base_url=base_url, timeout=5.0)

    def post(self, path: str, body: dict) -> dict:
        try:
            response = self._client.post(path, json=body)
        except httpx.TransportError as exc:
            raise ConnectionError(str(exc)) from exc
        response.raise_for_status()
        return response.json()
```

Registering a run and driving it end to end:

```python
import httpx

from quarantine.sdk import QuarantineClient, RunFrozen, ToolRefused

transport = HttpTransport("http://localhost:8000")

created = httpx.post(
    "http://localhost:8000/runs",
    json={
        "agent_name": "invoice-bot",
        "budget_tokens": 100_000,
        "budget_cost_cents": 500,
        "deadline_seconds": 3600,
    },
).json()

client = QuarantineClient(run_id=created["id"], transport=transport)
```

Wrap every tool call in `client.guard(...)` instead of calling the tool directly.
It gates the call, runs it only if permitted, and reports the outcome for you:

```python
def run_tool(tool_name: str, args: dict, args_digest: str):
    return client.guard(tool_name, args_digest, lambda: execute(tool_name, args))


try:
    result = run_tool("send_email", {"to": "ops@example.com"}, digest)
except ToolRefused:
    # The control plane said no to this one call -- DEGRADED agents are meant
    # to keep working. Fall back to something else instead of giving up.
    result = run_tool("search", {"q": "escalation contact"}, other_digest)
except RunFrozen:
    # The run itself is suspended. Stop -- a human has to release it.
    raise SystemExit("run frozen; awaiting operator review")
```

Send a `heartbeat()` periodically for long-running or between-tool-call gaps —
the reaper escalates a run whose worker goes quiet — and call `complete()` once
the agent is done so the run leaves the ladder cleanly:

```python
client.complete()
```

`heartbeat()`, `complete()`, and the outcome report inside `guard()` are all
best-effort: if the control plane is unreachable they swallow the
`ConnectionError` rather than crashing the agent on telemetry. An outage is
already handled by the reaper's silence detection and needs no cooperation from
the worker. `guard()` itself does **not** swallow errors from your own tool
action -- it reports the failure and re-raises, so your own exception handling
still sees it.

## Honest limitations

- **The protocol is cooperative.** A worker running modified code can skip the gate. The
  heartbeat reaper catches workers that go silent, but this is a safety mechanism against a
  *broken* agent, not a security boundary against a *hostile* one. Don't deploy it as the
  latter.
- **Freezing is not instantaneous.** A rule-triggered freeze lands on the next gate call. An
  unresponsive worker is caught within the heartbeat timeout, not immediately.
- **We gate tool calls; we don't sandbox the process.** An agent that bypasses its own tool
  layer and touches the filesystem directly is outside what this sees.
- **Single box.** No multi-tenancy, no HA. Internal tool.

## Project status

The design is documented, the decisions behind it are recorded, and the contract is
encoded as a test suite. All 182 tests pass. They were written from the spec before any
implementation existed, so building against them couldn't quietly redefine what the
system is supposed to do — the domain model, the gate, the LLM judge tiering, the
heartbeat reaper, the HTTP surface (both the worker protocol and the human-only operator
endpoints), persistence, and the worker SDK are all implemented and green.

Two things noted so nobody mistakes them for oversights: nothing runs the heartbeat
reaper on a schedule yet (`Reaper.sweep()` is implemented and tested; wiring it to a
periodic task is real work for its own change), and no HTTP transport ships with the
SDK — see [Using it from an agent](#using-it-from-an-agent) for the shape you supply.

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

| Document | What's in it |
|---|---|
| [PRD](docs/prd/2026-09-17-agent-quarantine.md) | The problem, users, scope, risks, and the user stories to build |
| [ADR-0001](docs/adr/0001-control-plane-with-cooperative-gate.md) | Why a separate control plane with a cooperative gate, rather than in-process or process-level kills |
| [ADR-0002](docs/adr/0002-graded-quarantine-ladder.md) | Why quarantine is a ladder and not a boolean |
| [ADR-0003](docs/adr/0003-tier-llm-judge-by-tool-risk.md) | Why the LLM judge is tiered by tool blast radius |

**The design's open questions are all resolved** (reasoning kept in
[PRD §8](docs/prd/2026-09-17-agent-quarantine.md#8-open-questions)): a `FROZEN` run needs a
human to release it while a `DEGRADED` run recovers on its own; unreachable dependencies
fail closed for high-risk tools and open for low-risk ones; release defaults to `DEGRADED`;
and the judge's verdict is binding.

### What the tests cover

| File | Covers |
|---|---|
| `tests/test_machine.py` | The transition table, exhaustively — every `(from, to)` pair is reachable exactly when permitted |
| `tests/test_gate.py` | The full decision matrix, judge placement, rule escalation, auto-recovery |
| `tests/test_rules.py` | The six detectors, table-driven |
| `tests/test_registry.py` | Tool classification, and that an unknown tool is `HIGH` |
| `tests/test_reaper.py` | Heartbeat escalation, on an injected clock |
| `tests/test_failure_modes.py` | The fail-open/fail-closed tiering under an unreachable judge |
| `tests/test_api.py` | HTTP surface and operator authorization |
| `tests/test_sqlite_store.py` | Persistence against the repository interface |
| `tests/test_sdk.py` | Worker SDK: gate/guard/report/heartbeat, and the client-side fail-open/fail-closed tiering |

## Stack

Python 3.14 · FastAPI · SQLite (WAL) behind a repository interface · pytest
