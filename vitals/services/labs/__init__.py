"""Application services for the Labs bounded context.

Import the owning leaf module explicitly.  The package root intentionally does
not aggregate commands: flags, marker identity, result persistence, alerts,
ingestion, and paid AI dispatch have different dependency and transaction
boundaries.
"""
