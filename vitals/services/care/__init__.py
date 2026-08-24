"""Care-team domain services.

The package owns the lifecycle around a professional entering a patient's
record: profile verification, invitations, relationships and consent, the
patient-visible record projection, and shared conversation threads.

Import the concept needed by the caller rather than importing this package as a
service locator, for example ``from vitals.services.care import relationships``.
"""
