"""Authorize one invitation without opening the installation-wide RLS scope.

Revision ID: 0081
Revises: 0080
Create Date: 2026-08-26

An invitation is subject-owned, but the professional accepting it cannot bind
that subject until the invitation has proved which subject it names.  The old
application path solved that bootstrap by enabling ``vitals.platform_scope``
and could therefore read every subject-owned table for the rest of the
transaction.

This routine is the deliberately smaller bridge.  It receives only the token's
SHA-256 digest and the accepting session's identity claims, locks the one row
that digest can name, and returns only that invitation and subject after every
acceptance check succeeds.  It neither changes an RLS setting nor returns
invitation metadata.  The application can then bind the ordinary subject scope
and continue through the forced policies.

The routine is owned by the migration role and executes with that role's
authority.  Its search path is fixed, every application relation is explicitly
qualified, and PUBLIC receives no execute privilege.  The runtime-role
provisioner grants this exact signature to the web login and no other routine.

Downgrade removes only the routine.  The existing RLS policy is unchanged, so
older application code using the platform clause remains rollback-safe.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0081"
down_revision: Union[str, None] = "0080"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ROUTINE_NAME = "authorize_and_lock_professional_invitation"
ROUTINE_ARGUMENTS = "text, uuid, text"
ROUTINE_SIGNATURE = f"public.{ROUTINE_NAME}({ROUTINE_ARGUMENTS})"

CREATE_ROUTINE_SQL = f"""
        CREATE FUNCTION public.{ROUTINE_NAME}(
            p_token_hash text,
            p_accepting_user_id uuid,
            p_verified_email text
        )
        RETURNS TABLE(invitation_id uuid, subject_id uuid)
        LANGUAGE plpgsql
        VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        SET row_security = off
        AS $vitals$
        DECLARE
            v_invitation_id uuid;
            v_subject_id uuid;
            v_status text;
            v_kind text;
            v_invited_email text;
            v_expires_at timestamptz;
            v_now timestamptz;
            v_user_status text;
            v_user_email text;
            v_email_verified_at timestamptz;
            v_owner_user_id uuid;
        BEGIN
            IF p_token_hash IS NULL
               OR p_accepting_user_id IS NULL
               OR p_accepting_user_id =
                    '00000000-0000-0000-0000-000000000000'::uuid
               OR pg_catalog.length(p_token_hash) <> 64
               OR p_token_hash !~ '^[0-9a-f]{{64}}$'
            THEN
                RETURN;
            END IF;

            SELECT invitation.id,
                   invitation.subject_id,
                   invitation.status,
                   invitation.kind,
                   invitation.invited_email,
                   invitation.expires_at
              INTO v_invitation_id,
                   v_subject_id,
                   v_status,
                   v_kind,
                   v_invited_email,
                   v_expires_at
              FROM public.professional_invitations AS invitation
             WHERE invitation.token_hash = p_token_hash
             FOR UPDATE;

            IF NOT FOUND OR v_status <> 'pending' THEN
                RETURN;
            END IF;

            v_now := pg_catalog.transaction_timestamp();
            IF v_now >= v_expires_at THEN
                UPDATE public.professional_invitations AS invitation
                   SET status = 'expired',
                       updated_at = v_now
                 WHERE invitation.id = v_invitation_id;
                RETURN;
            END IF;

            IF p_verified_email IS NULL
               OR v_invited_email IS DISTINCT FROM p_verified_email
            THEN
                RETURN;
            END IF;

            SELECT user_account.status,
                   user_account.normalized_email,
                   user_account.email_verified_at
              INTO v_user_status,
                   v_user_email,
                   v_email_verified_at
              FROM public.users AS user_account
             WHERE user_account.id = p_accepting_user_id;
            IF NOT FOUND
               OR v_user_status <> 'active'
               OR v_email_verified_at IS NULL
               OR v_user_email IS DISTINCT FROM p_verified_email
            THEN
                RETURN;
            END IF;

            IF NOT EXISTS (
                SELECT 1
                  FROM public.user_roles AS account_role
                 WHERE account_role.user_id = p_accepting_user_id
                   AND account_role.role = v_kind
            ) THEN
                RETURN;
            END IF;

            IF NOT EXISTS (
                SELECT 1
                  FROM public.professional_profiles AS profile
                 WHERE profile.user_id = p_accepting_user_id
                   AND profile.kind = v_kind
                   AND profile.verification_status = 'verified'
            ) THEN
                RETURN;
            END IF;

            SELECT subject.owner_user_id
              INTO v_owner_user_id
              FROM public.health_subjects AS subject
             WHERE subject.id = v_subject_id;
            IF NOT FOUND OR v_owner_user_id = p_accepting_user_id THEN
                RETURN;
            END IF;

            invitation_id := v_invitation_id;
            subject_id := v_subject_id;
            RETURN NEXT;
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
