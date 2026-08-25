"""Account admission by invitation or explicit operator approval.

The package owns the two deliberately narrow ways an unknown federated identity
may become a local account without enabling open registration. It never
commits: the OIDC callback, operator route, or maintenance job owns the
transaction. Identity mutations take the shared governance lock before row
locks, and member provisioning enters explicit platform RLS scope because the
new identity cannot have subject context until its graph exists.

Expiry is lazy. A refusing call may have transitioned an expired row before it
raises; callers that want that bookkeeping persisted must commit the safe
terminal transition after handling the refusal. Correctness does not depend on
that commit because every later reader checks the deadline again.
"""

from vitals.services.authentication.admission._shared import (
    DEFAULT_RETENTION,
    INVITATION_TTL,
    MAX_INVITATION_TTL,
    MAX_MAINTENANCE_BATCH,
    MAX_REQUEST_TTL,
    MINIMUM_RETENTION,
    REQUEST_REAPPLY_COOLDOWN,
    REQUEST_TTL,
    AdmissionError,
    AdmissionForbidden,
    AdmissionRefused,
    AdmissionResult,
    AdmissionStateError,
    AdmissionValidationError,
    IssuedInvitation,
    RetentionResult,
)
from vitals.services.authentication.admission.invitations import (
    claim_invitation,
    consume_invitation,
    consume_invitation_claim,
    issue_invitation,
    revoke_invitation,
)
from vitals.services.authentication.admission.requests import (
    approve_request,
    get_request,
    reject_request,
    submit_request,
)
from vitals.services.authentication.admission.retention import (
    expire_due,
    purge_terminal,
)

__all__ = [
    "DEFAULT_RETENTION",
    "INVITATION_TTL",
    "MAX_INVITATION_TTL",
    "MAX_MAINTENANCE_BATCH",
    "MAX_REQUEST_TTL",
    "MINIMUM_RETENTION",
    "REQUEST_REAPPLY_COOLDOWN",
    "REQUEST_TTL",
    "AdmissionError",
    "AdmissionForbidden",
    "AdmissionRefused",
    "AdmissionResult",
    "AdmissionStateError",
    "AdmissionValidationError",
    "IssuedInvitation",
    "RetentionResult",
    "approve_request",
    "claim_invitation",
    "consume_invitation",
    "consume_invitation_claim",
    "expire_due",
    "get_request",
    "issue_invitation",
    "purge_terminal",
    "reject_request",
    "revoke_invitation",
    "submit_request",
]
