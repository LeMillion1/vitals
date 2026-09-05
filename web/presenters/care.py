"""Presentation mapping for the professional care workspace."""

from __future__ import annotations

from vitals.services.care.workspace import ProfessionalWorkspace


def _initial_display_name(username: str) -> str:
    """Do not suggest an email address as a patient-facing name."""

    candidate = username.strip()
    local, separator, domain = candidate.partition("@")
    if (
        separator
        and local
        and domain
        and not any(char.isspace() for char in candidate)
    ):
        return ""
    return username


def professional_roster_context(
    workspace: ProfessionalWorkspace,
    *,
    username: str,
    accepted: bool = False,
    submitted: bool = False,
    profile_error: str | None = None,
    display_name: str | None = None,
    credential_reference: str | None = None,
) -> dict[str, object]:
    """Build the one render contract shared by roster and onboarding states."""

    profile = workspace.profile
    return {
        "patients": workspace.patients,
        "username": username,
        "accepted": accepted,
        "submitted": submitted,
        "professional_profile": profile,
        "onboarding_kind": (
            workspace.onboarding_kind.value
            if workspace.onboarding_kind is not None
            else None
        ),
        "profile_verified": workspace.profile_verified,
        "is_professional_account": bool(workspace.professional_roles),
        "active_account_nav": "professional_care",
        "profile_error": profile_error,
        "profile_form": {
            "display_name": (
                display_name
                if display_name is not None
                else (
                    profile.display_name
                    if profile is not None
                    else _initial_display_name(username)
                )
            ),
            "credential_reference": (
                credential_reference
                if credential_reference is not None
                else (
                    profile.credential_reference
                    if profile is not None and profile.credential_reference
                    else ""
                )
            ),
        },
    }


__all__ = ["professional_roster_context"]
