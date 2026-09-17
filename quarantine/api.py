"""FastAPI surface (spec §6).

Two audiences with different authorisation: the worker protocol endpoints, which any
registered worker calls, and the operator endpoints, which require a human. Releasing
a FROZEN run is the operation that must never become automatic (ADR-0002).
"""

from __future__ import annotations

from fastapi import FastAPI


def create_app(store, judge, clock, settings) -> FastAPI:
    """Build the application against injected collaborators.

    Nothing is constructed at import time, so tests can wire an in-memory repository
    and a fake judge without touching a database or a model.

    Worker protocol:
        POST /runs                  register a run
        POST /runs/{id}/gate        the gate (spec §5)
        POST /runs/{id}/outcome     report a completed tool call
        POST /runs/{id}/heartbeat   liveness
        POST /runs/{id}/complete    finished normally -> TERMINATED

    Operator (human actor required):
        GET  /runs                  list, filterable by state
        GET  /runs/{id}             detail with transition history
        GET  /runs/{id}/trajectory  the event log
        POST /runs/{id}/release     target state, defaults to DEGRADED
        POST /runs/{id}/terminate
    """
    raise NotImplementedError("api.create_app")
