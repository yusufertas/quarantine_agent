"""FastAPI surface (spec §6).

Two audiences with different authorisation: the worker protocol endpoints, which any
registered worker calls, and the operator endpoints, which require a human. Releasing
a FROZEN run is the operation that must never become automatic (ADR-0002).
"""

# NO `from __future__ import annotations` IN THIS MODULE -- deliberately.
# Under PEP 563 every annotation becomes a string that FastAPI resolves against
# the module globals, and `Operator` is a closure local defined inside
# create_app(). It would resolve to nothing, FastAPI would treat `actor` as an
# ordinary query parameter, and all five operator routes would 422 instead of
# authenticating. A lint-driven consistency pass adding it here breaks the
# operator surface silently; tests/test_operator_surface.py is the tripwire.
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .domain import machine
from .domain.models import Event, Run, ToolCall, Transition
from .domain.states import EventKind, RunState
from .errors import (
    AuthorizationRequired,
    IllegalTransition,
    RunTerminated,
    StoreUnavailable,
    UnknownRun,
)
from .gate import Gate


class RegisterRun(BaseModel):
    agent_name: str
    budget_tokens: int
    budget_cost_cents: int
    deadline_seconds: int


class GateRequest(BaseModel):
    tool_name: str
    args_digest: str
    args_preview: str = ""


class OutcomeRequest(BaseModel):
    tool_name: str
    ok: bool
    tokens: int = 0
    cost_cents: int = 0


class ReleaseRequest(BaseModel):
    target: RunState = RunState.DEGRADED
    detail: str = ""


class TerminateRequest(BaseModel):
    detail: str = ""


def _run_dict(run: Run) -> dict:
    """Serialize a run for the operator surface, budgets and deadline included.

    The counters alone do not answer the question an operator actually arrives
    with -- "why was this frozen?". `tokens_used: 104312` means nothing next to
    `budget_tokens: 100000`, and a `wall_clock` escalation is unreadable without
    `deadline_at`. The limits travel with the counters so the reason a rule fired
    is legible from this payload alone, without a second lookup.
    """
    return {
        "id": run.id,
        "agent_name": run.agent_name,
        "state": run.state.value,
        "state_since": run.state_since.isoformat(),
        "created_at": run.created_at.isoformat(),
        "last_heartbeat_at": run.last_heartbeat_at.isoformat(),
        "deadline_at": run.deadline_at.isoformat(),
        "tokens_used": run.tokens_used,
        "budget_tokens": run.budget_tokens,
        "cost_cents": run.cost_cents,
        "budget_cost_cents": run.budget_cost_cents,
        "tool_calls": run.tool_calls,
        "consecutive_errors": run.consecutive_errors,
    }


def _transition_dict(t: Transition) -> dict:
    return {
        "from": t.from_state.value,
        "to": t.to_state.value,
        "cause": t.cause,
        "actor": t.actor,
        "detail": t.detail,
        "at": t.created_at.isoformat(),
    }


def _event_dict(e: Event) -> dict:
    return {
        "seq": e.seq,
        "kind": e.kind.value,
        "tool_name": e.tool_name,
        "args_digest": e.args_digest,
        "payload": e.payload,
        "at": e.created_at.isoformat(),
    }


def create_app(
    store,
    judge,
    clock,
    settings,
    background: Callable[[Callable[[], None]], None] | None = None,
) -> FastAPI:
    app = FastAPI(title="Agent Quarantine")
    gate = Gate(
        store=store, judge=judge, clock=clock, settings=settings, background=background
    )

    # Errors that carry design meaning map to status codes, never to a
    # permissive default. A gate call against a terminated run is 409, not a
    # FREEZE, because it is a bug in the caller.
    @app.exception_handler(UnknownRun)
    def _unknown(_, exc):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(RunTerminated)
    def _terminated(_, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(IllegalTransition)
    def _illegal(_, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(AuthorizationRequired)
    def _unauthorized(_, exc):
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    # 503 rather than the 500 an unhandled exception would produce: spec §9 makes
    # a store outage a tiering event, and the SDK can only tier client-side on a
    # status that says "the control plane cannot decide right now" rather than
    # "the control plane is broken".
    @app.exception_handler(StoreUnavailable)
    def _store_down(_, exc):
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    def operator(
        x_operator_token: Annotated[str | None, Header()] = None,
        x_operator_id: Annotated[str | None, Header()] = None,
    ) -> str:
        """Identify the human behind an operator request.

        There is no fallback to a system actor: a worker must never be able to
        release itself, and a missing credential is a rejection rather than a
        downgrade.
        """
        if not settings.operator_token or x_operator_token != settings.operator_token:
            raise HTTPException(status_code=401, detail="operator credentials required")
        if not x_operator_id:
            raise HTTPException(status_code=401, detail="operator id required")
        return f"human:{x_operator_id}"

    Operator = Annotated[str, Depends(operator)]

    # ---- worker protocol -------------------------------------------------

    @app.post("/runs", status_code=201)
    def register(body: RegisterRun) -> dict:
        now = clock.now()
        run = Run(
            id=str(uuid4()),
            agent_name=body.agent_name,
            state=RunState.HEALTHY,
            state_since=now,
            created_at=now,
            last_heartbeat_at=now,
            budget_tokens=body.budget_tokens,
            budget_cost_cents=body.budget_cost_cents,
            deadline_at=now + timedelta(seconds=body.deadline_seconds),
        )
        store.create_run(run)
        return {"id": run.id}

    @app.post("/runs/{run_id}/gate")
    def gate_call(run_id: str, body: GateRequest) -> dict:
        decision = gate.decide(
            run_id,
            ToolCall(
                tool_name=body.tool_name,
                args_digest=body.args_digest,
                args_preview=body.args_preview,
            ),
        )
        return {"decision": decision.value}

    @app.post("/runs/{run_id}/outcome")
    def outcome(run_id: str, body: OutcomeRequest) -> dict:
        """Record a tool result and advance the run's counters.

        This is a read-modify-write of a snapshot, and deliberately safe as one:
        `save_run` cannot write `state`, so an escalation landing between the read
        and the write survives untouched. Before that constraint existed, a report
        arriving a millisecond after a freeze wrote the pre-freeze state back and
        un-contained the run while its transition row stayed on the record.
        """
        run = store.get_run(run_id)
        now = clock.now()
        store.save_run(
            replace(
                run,
                tokens_used=run.tokens_used + body.tokens,
                cost_cents=run.cost_cents + body.cost_cents,
                tool_calls=run.tool_calls + 1,
                consecutive_errors=0 if body.ok else run.consecutive_errors + 1,
                last_heartbeat_at=now,
            )
        )
        store.append_event(
            Event(
                run_id=run_id,
                seq=store.next_seq(run_id),
                kind=EventKind.OUTCOME,
                created_at=now,
                tool_name=body.tool_name,
                payload={
                    "ok": body.ok,
                    "tokens": body.tokens,
                    "cost_cents": body.cost_cents,
                },
            )
        )
        return {"ok": True}

    @app.post("/runs/{run_id}/heartbeat")
    def heartbeat(run_id: str) -> dict:
        """Mark the worker alive. Liveness only -- never a state change.

        Same property as `outcome`: `save_run` carries no `state`, so a heartbeat
        cannot resurrect a run the machine has since contained.
        """
        run = store.get_run(run_id)
        store.save_run(replace(run, last_heartbeat_at=clock.now()))
        return {"ok": True}

    @app.post("/runs/{run_id}/complete")
    def complete(run_id: str) -> dict:
        run = store.get_run(run_id)
        updated, transition = machine.complete(run, now=clock.now())
        store.record_state_change(updated, transition)
        return {"state": updated.state.value}

    # ---- operator surface (human actor required) -------------------------

    @app.get("/runs")
    def list_runs(actor: Operator, state: RunState | None = None) -> list[dict]:
        return [_run_dict(run) for run in store.list_runs(state)]

    @app.get("/runs/{run_id}")
    def run_detail(run_id: str, actor: Operator) -> dict:
        run = store.get_run(run_id)
        return {
            **_run_dict(run),
            "transitions": [_transition_dict(t) for t in store.transitions(run_id)],
        }

    @app.get("/runs/{run_id}/trajectory")
    def trajectory(run_id: str, actor: Operator) -> list[dict]:
        store.get_run(run_id)          # 404 for an unknown run
        return [_event_dict(e) for e in store.recent_events(run_id, 1000)]

    @app.post("/runs/{run_id}/release")
    def release(
        run_id: str, actor: Operator, body: ReleaseRequest = ReleaseRequest()
    ) -> dict:
        """Release a FROZEN run. An absent body means the documented default.

        `body` carries a default instance so `POST /release` with no body at all
        lands on `target=DEGRADED` -- documenting a default that only applies if
        you remember to send `{}` is not a default.
        """
        run = store.get_run(run_id)
        updated, transition = machine.release(
            run, target=body.target, actor=actor, now=clock.now(), detail=body.detail
        )
        store.record_state_change(updated, transition)
        return {"state": updated.state.value}

    @app.post("/runs/{run_id}/terminate")
    def terminate(run_id: str, actor: Operator, body: TerminateRequest) -> dict:
        run = store.get_run(run_id)
        updated, transition = machine.terminate(
            run, actor=actor, now=clock.now(), detail=body.detail
        )
        store.record_state_change(updated, transition)
        return {"state": updated.state.value}

    return app
