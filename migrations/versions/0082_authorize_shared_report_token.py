"""Authorize one public report token without opening the platform scope.

Revision ID: 0082
Revises: 0081
Create Date: 2026-08-26

An anonymous report reader has no account from which to resolve an ordinary
subject scope.  The previous application path therefore enabled
``vitals.platform_scope`` before looking up the bearer token, leaving every
subject-owned table visible for the rest of that transaction.

This routine is the smaller bootstrap boundary.  It accepts one bounded token
and returns a non-PHI attestation sufficient to validate subject ownership,
owner liveness, report liveness, and the historical migration checkpoint before
binding that subject.  It does not expose the token, password hash, title,
snapshot, note, domains, or any medical data.  The application binds the
subject only after validating the attestation, then repeats every check under
ordinary forced RLS before rendering or incrementing an open count.

The routine is migration-owned, has a fixed catalog-only search path, disables
row security only inside its body, and is not executable by PUBLIC.  Runtime
role provisioning grants this exact signature and treats every other routine as
unexpected authority.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0082"
down_revision: Union[str, None] = "0081"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ROUTINE_NAME = "attest_shared_report_token"
ROUTINE_ARGUMENTS = "text"
ROUTINE_SIGNATURE = f"public.{ROUTINE_NAME}({ROUTINE_ARGUMENTS})"

CREATE_ROUTINE_SQL = f"""
        CREATE FUNCTION public.{ROUTINE_NAME}(p_token text)
        RETURNS TABLE(
            report_id integer,
            subject_id uuid,
            created_by_user_id uuid,
            revoked_by_user_id uuid,
            revoked_at timestamp without time zone,
            expires_at timestamp without time zone,
            has_snapshot boolean,
            owner_user_id uuid,
            owner_status text,
            checkpoint_phase_key text,
            checkpoint_subject_id uuid,
            checkpoint_status text,
            checkpoint_scan_high_watermark_id bigint,
            checkpoint_snapshot_rows bigint,
            checkpoint_last_scanned_id bigint,
            checkpoint_scanned_rows bigint,
            checkpoint_updated_rows bigint,
            checkpoint_unchanged_rows bigint,
            checkpoint_data_checksum_before text,
            checkpoint_data_checksum_after text,
            checkpoint_ownership_checksum_after text,
            checkpoint_started_at timestamp with time zone,
            checkpoint_updated_at timestamp with time zone,
            checkpoint_completed_at timestamp with time zone
        )
        LANGUAGE plpgsql
        VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        SET row_security = off
        AS $vitals$
        BEGIN
            IF p_token IS NULL
               OR p_token = ''
               OR p_token <> pg_catalog.btrim(p_token)
               OR pg_catalog.length(p_token) > 64
            THEN
                RETURN;
            END IF;

            RETURN QUERY
            SELECT report.id,
                   report.subject_id,
                   report.created_by_user_id,
                   report.revoked_by_user_id,
                   report.revoked_at,
                   report.expires_at,
                   report.snapshot IS NOT NULL,
                   subject.owner_user_id,
                   owner.status::text,
                   checkpoint.phase_key::text,
                   checkpoint.subject_id,
                   checkpoint.status::text,
                   checkpoint.scan_high_watermark_id,
                   checkpoint.snapshot_rows,
                   checkpoint.last_scanned_id,
                   checkpoint.scanned_rows,
                   checkpoint.updated_rows,
                   checkpoint.unchanged_rows,
                   checkpoint.data_checksum_before::text,
                   checkpoint.data_checksum_after::text,
                   checkpoint.ownership_checksum_after::text,
                   checkpoint.started_at,
                   checkpoint.updated_at,
                   checkpoint.completed_at
              FROM public.shared_reports AS report
              JOIN public.health_subjects AS subject
                ON subject.id = report.subject_id
              JOIN public.users AS owner
                ON owner.id = subject.owner_user_id
         LEFT JOIN public.ownership_backfill_checkpoints AS checkpoint
                ON checkpoint.phase_key =
                   'stage3.retained_artifact.shared_reports.v1.shared_reports'
             WHERE report.token = p_token;
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
