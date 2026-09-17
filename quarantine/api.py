"""FastAPI surface (spec §6).

Two audiences with different authorisation: the worker protocol endpoints, which any
registered worker calls, and the operator endpoints, which require a human. Releasing
a FROZEN run is the operation that must never become automatic (ADR-0002).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .domain import machine
from .domain.models import Event, Run, ToolCall
from .domain.states import EventKind, RunState
from .errors import AuthorizationRequired, IllegalTransition, RunTerminated, UnknownRun
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
    return {
        "id": run.id,
        "agent_name": run.agent_name,
        "state": run.state.value,
        "state_since": run.state_since.isoformat(),
        "last_heartbeat_at": run.last_heartbeat_at.isoformat(),
        "tokens_used": run.tokens_used,
        "cost_cents": run.cost_cents,
        "tool_calls": run.tool_calls,
        "consecutive_errors": run.consecutive_errors,
    }


def _transition_dict(t) -> dict:
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
        run = store.get_run(run_id)
        store.save_run(replace(run, last_heartbeat_at=clock.now()))
        return {"ok": True}

    @app.post("/runs/{run_id}/complete")
    def complete(run_id: str) -> dict:
        run = store.get_run(run_id)
        updated, transition = machine.complete(run, now=clock.now())
        store.save_run(updated)
        store.append_transition(transition)
        return {"state": updated.state.value}

    return app
