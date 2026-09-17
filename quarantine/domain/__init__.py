"""Pure domain logic. No I/O lives here.

That constraint is load-bearing twice over: it keeps the LOW-risk gate path free of
network hops (ADR-0003) and it makes the rules testable without fixtures (spec §11).
"""
