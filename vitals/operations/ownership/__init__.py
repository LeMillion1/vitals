"""One-shot and resumable subject-ownership transition operations.

These modules are deployment and data-transition programs. They deliberately
live outside :mod:`vitals.services`: request-time domain services may consult a
validated historical bridge, but backfill orchestration is not request-time
business behavior.
"""
