"""Channel-neutral ownership envelope for proactive capture and delivery."""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from vitals.ownership import WriteIdentity


@dataclass(frozen=True, slots=True)
class ProactiveOwnershipContext:
    """Subject, recipient, and delivery roots for one proactive operation.

    The context deliberately does not name a vendor.  Channel-specific code
    resolves which connection backs the active notifier, then every composer,
    policy, and journal function sees only this neutral contract.
    """

    subject_id: uuid.UUID
    recipient_user_id: uuid.UUID
    connection_id: uuid.UUID
    include_legacy_unowned: bool = False

    def __post_init__(self) -> None:
        for field_name in (
            "subject_id",
            "recipient_user_id",
            "connection_id",
        ):
            if not isinstance(getattr(self, field_name), uuid.UUID):
                raise TypeError(f"{field_name} must be a UUID")
        if not isinstance(self.include_legacy_unowned, bool):
            raise TypeError("include_legacy_unowned must be a bool")

    def owner_action(self) -> WriteIdentity:
        """An authenticated inbound human action by the channel recipient."""

        return WriteIdentity(
            subject_id=self.subject_id,
            actor_user_id=self.recipient_user_id,
        )

    def system_action(self) -> WriteIdentity:
        """A scheduled or automatically composed action without a human actor."""

        return WriteIdentity(subject_id=self.subject_id, actor_user_id=None)


__all__ = ["ProactiveOwnershipContext"]
