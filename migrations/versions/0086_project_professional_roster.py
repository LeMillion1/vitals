"""Expose one actor's bounded professional roster before subject binding.

Revision ID: 0086
Revises: 0085
Create Date: 2026-09-05

A professional's roster is necessarily a cross-subject index: the application
cannot bind a patient's subject until it knows which patients belong to the
signed-in professional.  Forced row-level security correctly makes the ordinary
unbound query empty, while granting the web login installation-wide scope would
make every subject table visible for the rest of the transaction.

This security-definer routine is the deliberately smaller bridge.  It accepts
only the authenticated application's actor UUID, validates an active account
and the exact relationship kind's assigned role and verified profile, and
returns only the non-ended relationship fields used by the roster.  Conversation
contents are never returned.  Even aggregate message metadata is projected only
for a relationship with active, unexpired consent carrying the exact message
read scope; closed or health-only records receive zero and NULL inside the
privileged boundary.

The routine is owned by the migration role, has a fixed catalog-only search
path, fully qualifies every application relation, and is not executable by
PUBLIC.  Runtime provisioning grants this exact signature only to the web role.
No RLS policy or installation-wide capability changes.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0086"
down_revision: Union[str, None] = "0085"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ROUTINE_NAME = "project_professional_roster"
ROUTINE_ARGUMENTS = "uuid"
ROUTINE_SIGNATURE = f"public.{ROUTINE_NAME}({ROUTINE_ARGUMENTS})"

CREATE_ROUTINE_SQL = f"""
        CREATE FUNCTION public.{ROUTINE_NAME}(p_professional_user_id uuid)
        RETURNS TABLE(
            id uuid,
            subject_id uuid,
            kind text,
            status text,
            display_name text,
            consent_status text,
            expires_at timestamp with time zone,
            unread_threads bigint,
            last_message_at timestamp with time zone
        )
        LANGUAGE plpgsql
        VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        SET row_security = off
        AS $vitals$
        DECLARE
            v_now timestamp with time zone;
        BEGIN
            IF p_professional_user_id IS NULL
               OR p_professional_user_id =
                    '00000000-0000-0000-0000-000000000000'::uuid
               OR NOT EXISTS (
                    SELECT 1
                      FROM public.users AS actor
                     WHERE actor.id = p_professional_user_id
                       AND actor.status = 'active'
               )
            THEN
                RETURN;
            END IF;

            v_now := pg_catalog.transaction_timestamp();
            RETURN QUERY
            SELECT relationship.id,
                   relationship.subject_id,
                   relationship.kind::text,
                   relationship.status::text,
                   subject.display_name::text,
                   consent.status::text,
                   consent.expires_at,
                   CASE
                        WHEN relationship.status = 'active'
                        AND consent.status = 'active'
                        AND consent.expires_at > v_now
                        AND message_access.may_read
                       THEN (
                           SELECT pg_catalog.count(participant.id)
                             FROM public.care_thread_participants AS participant
                            WHERE participant.relationship_id = relationship.id
                              AND participant.user_id = p_professional_user_id
                              AND participant.removed_at IS NULL
                              AND EXISTS (
                                  SELECT 1
                                    FROM public.care_messages AS unread_message
                                   WHERE unread_message.thread_id =
                                         participant.thread_id
                                     AND unread_message.actor_user_id <>
                                         p_professional_user_id
                                     AND unread_message.created_at >
                                         participant.last_read_at
                              )
                       )
                       ELSE 0::bigint
                   END AS unread_threads,
                   CASE
                        WHEN relationship.status = 'active'
                        AND consent.status = 'active'
                        AND consent.expires_at > v_now
                        AND message_access.may_read
                       THEN (
                           SELECT pg_catalog.max(latest_message.created_at)
                             FROM public.care_thread_participants
                                  AS latest_participant
                             JOIN public.care_messages AS latest_message
                               ON latest_message.thread_id =
                                  latest_participant.thread_id
                            WHERE latest_participant.relationship_id =
                                  relationship.id
                              AND latest_participant.user_id =
                                  p_professional_user_id
                              AND latest_participant.removed_at IS NULL
                       )
                       ELSE NULL::timestamp with time zone
                   END AS last_message_at
              FROM public.care_relationships AS relationship
              JOIN public.health_subjects AS subject
                ON subject.id = relationship.subject_id
              JOIN public.user_roles AS account_role
                ON account_role.user_id = p_professional_user_id
               AND account_role.role = relationship.kind
              JOIN public.professional_profiles AS profile
                ON profile.user_id = p_professional_user_id
               AND profile.kind = relationship.kind
               AND profile.verification_status = 'verified'
         LEFT JOIN public.consent_grants AS consent
                ON consent.relationship_id = relationship.id
               AND consent.subject_id = relationship.subject_id
               AND consent.status IN ('active', 'paused')
         LEFT JOIN LATERAL (
                   SELECT true AS may_read
                     FROM public.consent_scopes AS scope
                    WHERE scope.consent_grant_id = consent.id
                      AND scope.subject_id = relationship.subject_id
                      AND scope.resource_type = 'operation'
                      AND scope.resource_key = 'care_team.message'
                      AND scope.action = 'read'
                    LIMIT 1
               ) AS message_access ON true
             WHERE relationship.professional_user_id = p_professional_user_id
               AND relationship.status <> 'ended'
             ORDER BY subject.display_name, relationship.id;
        END;
        $vitals$
        """


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(CREATE_ROUTINE_SQL)
    op.execute(f"REVOKE ALL ON FUNCTION {ROUTINE_SIGNATURE} FROM PUBLIC")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(f"DROP FUNCTION IF EXISTS {ROUTINE_SIGNATURE}")
