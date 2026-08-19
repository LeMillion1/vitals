# Commercial Legacy Dual-write Matrix

Status: PR-03 Stage-2 implementation source of truth

Last reviewed: 2026-08-19

This document records every compatibility write boundary that must populate the
nullable ownership columns introduced by revisions `0037` and `0038`. It is the
runtime companion to `COMMERCIAL_OWNERSHIP_INVENTORY.md`; that inventory owns the
schema target, while this file owns how new writes reach it before registration
or multi-subject reads are enabled.

## Boundary contract

- Resolve the sole legacy health subject and active owner once at a transaction
  boundary. A human web action must match the authenticated normalized username.
- Pass an immutable `WriteIdentity(subject_id, actor_user_id)` into domain write
  services. A background operation uses `actor_user_id = NULL`; a missing actor
  is never interpreted inside a service as "probably the owner".
- Resolve only the integration roots required by the operation. A provider row
  may be legacy, pending, active, or disabled and still be valid provenance, but
  a retired, missing, or ambiguous root fails closed.
- `Source` remains ingestion provenance and never substitutes for subject, actor,
  connection, file, relationship, or consent identity.
- A legacy row with `subject_id IS NULL` may be attached to the sole legacy
  subject during a reviewed reconcile. A row already attached to another subject
  is never reassigned. Historical actor fields remain unchanged.
- Domain services receive explicit values. ORM autofill hooks are forbidden:
  they cannot safely classify global catalogs, Core SQL, provider identity,
  inherited children, files, or lifecycle actors.

## Write-path matrix

| Tables | Runtime writers | Stage-2 ownership rule |
| --- | --- | --- |
| `annotations` | timeline web/service and MCP event/note tools | New human rows get S+A; updates retain A and require the same S. |
| `weight_logs` | manual/MCP saves, Garmin bridge, body-scan bridge | S always; human writes get A, provider writes get provider C, derived writes retain source ownership. |
| `body_measurements` | manual/MCP and lean-mass recompute | Human creates get S+A; derived recompute preserves ownership. |
| `progress_photos` | protected upload/weight service | S+A+F; the file asset is registered before the fact row. |
| `noise_markers` | web/MCP | New rows get S+A; delete is S-scoped. |
| `body_scans`, `body_scan_metrics` | upload confirm, structured MCP, raw reparse | Scan ownership comes from the trusted upload/raw boundary; metrics copy S from the scan. |
| `conflict_rules` | curated catalog sync and subject toggle | Curated rows stay global. Subject activation moves to `SubjectSetting` with temporary legacy dual-write. |
| `signals`, `day_context` | Telegram, MCP, evening plan, raw reparse | Telegram facts get S+A+Telegram C; MCP gets S+A; planned/system rows have A/C null; reparse copies raw ownership. |
| `garmin_daily`, `garmin_activities`, `garmin_intraday` | scheduler, on-demand sync, HAE import, raw reparse | S+Garmin C required. Human-triggered runs get A; scheduler runs do not. Intraday replacement deletes only inside S+C. |
| `garmin_weight_exports` | Core upsert and outbox lifecycle | S comes from the weight, C is the Garmin destination, and requester records the human who initiated it. Lifecycle updates never erase requester. |
| `genetic_variants` | VCF/manual/MCP | Raw-first, then S+A and raw link on interpreted variants. Upsert keys are S-scoped. |
| `raw_payloads` | all imports/connectors/uploads/Telegram | Raw ownership is written before normalized rows. Lookup is S/C scoped; refresh preserves historical A and rejects cross-S/C/F conflicts. |
| `glp1_*` | web/MCP | Human rows get S+A; automatic phase close is S-scoped and preserves A. MCP retains `Source.MCP`. |
| `hevy_workouts`, `hevy_exercises`, `hevy_sets` | sync and raw reparse | Workout gets S+Hevy C; children copy S+C from the parent. Child rebuild/delete is parent-scoped. |
| `hrt_compounds`, `hrt_compound_components` | catalog sync and activation | Curated definitions/components remain global; subject activation is a scoped setting. Future custom rows and components share S. |
| `hrt_doses`, `hrt_side_effects`, `hrt_cycles`, `hrt_cycle_items`, `hrt_cycle_templates`, `hrt_cycle_template_items` | web/MCP/template import/materialization | Human roots get S+A; child rows copy S; referenced compounds must be global or same-S. Automatic closes preserve A. |
| `lab_markers`, `lab_results` | upload/parser, manual/MCP, reparse, hormone seed | S always; human/import parser rows get A, system seed does not. Raw and result S must match. |
| `meal_logs` | web/MCP | S+A and existing MCP provenance. |
| `milestones` | reports web/MCP | Create gets S+A; updates retain A. |
| `weekly_digests` | web/MCP/schedulers/brief | S always; human generation gets A, scheduler does not. OpenRouter C is set only when that provider actually produced content. |
| `notifications` | proactive delivery | S + recipient user + Telegram C; explicit human test actions may get A, scheduled/reply delivery does not infer one. |
| `shared_reports` | create/open/revoke/purge | Create gets S+creator; human revoke gets revoker. Anonymous open and scheduled purge do not mutate actor fields. |
| `skincare_*` | web/MCP/seed scripts | Human creates get S+A; product/log updates preserve A and are S-scoped. |
| `supplements` | web/MCP | Human creates get S+A; updates retain A and MCP retains `Source.MCP`. |
| `system_alerts` | domain services, jobs, web/MCP lifecycle | Health alerts get S; provider alerts also get C. Human override/resolve uses the named actor field; automatic resolution remains actorless. |

Direct MCP note updates to weight, meals, GLP-1, skincare, body measurements,
body scans, and labs must go through owned services or perform an explicit
same-subject assertion. A bare primary key is never a write authority.

## Raw-first provider matrix

| Origin | Raw S/A/C/F | Normalized inheritance |
| --- | --- | --- |
| Garmin API / Health Auto Export | S; optional triggering A; Garmin C; no F | Daily/activity/intraday copy S+A+C. |
| Hevy | S; optional triggering A; Hevy C; no F | Workout copies S+A+C; exercises/sets copy S+C. |
| Telegram | S+A+Telegram C; no F | Signals/day context copy the raw ownership. |
| Lab document | S+A+OpenRouter C+lab F when AI parsing ran | Results/markers use raw S/A; F remains on raw. |
| Body-scan document | S+A+OpenRouter C+body F when AI parsing ran | Scan uses raw S/A/F; metrics copy S. |
| Structured MCP lab/body input | S+A; C/F null | Normalized rows use the supplied write identity. |
| VCF | S+A; C/F null | Curated variants link to that raw row. |

Upload confirmation must load the raw/file rows by S and must not trust a client
pair of IDs or a client-provided storage key. VCF, backup JSON, Garmin HAE import,
and HRT template JSON are parse-only inputs and are not `FileAsset` objects.

## Scoped setting migration

The first reviewed mappings are deliberately small:

- `ui_language` -> `UserSetting`;
- `enabled_modules`, `custom_charts`, `week_template` -> `SubjectSetting`;
- `garmin_weight_export_enabled` -> Garmin `IntegrationConnectionSetting`.

Reads are new-first with legacy fallback. Writes update both rows in one caller-
owned transaction. `twofa_secret`, credentials, token material, unknown keys, and
the mixed `proactive` object are not copied by a generic bridge. The proactive
object must first be split into subject, Telegram-connection, and Garmin-
connection fields. Redis keys must include the corresponding user/S/C UUID before
a second subject exists.

## File transition

`FileAsset` registration is metadata-only and must not read or move bytes. The
legacy relative paths are limited to:

- `uploads/` for progress photos;
- `labs/` for lab documents;
- `body/` for body-scan documents.

Registration records a safe relative path, purpose, optional uploader, already-
known media type/size/SHA-256, and `legacy_placeholder` lifecycle. Repeated
registration is idempotent only for the same subject, purpose, and compatible
metadata. Delete/purge transitions update lifecycle timestamps; they do not hard-
delete the ownership root.

## Completion gates

- Every production constructor, Core insert/upsert, and bulk update has a reviewed
  ownership call site; a static inventory test fails when a new path appears.
- One create test covers each ownership-bearing table and actor/channel policy.
- Raw and normalized S match; required provider C matches; direct children copy
  parent S/C; cross-subject repair is rejected.
- Backup v1 rebinds S, derives child S/C, maps required legacy connections,
  creates safe file placeholders, leaves actors null, and mirrors known scoped
  settings atomically.
- Scripts that perform global delete/import either require the sole legacy
  context and scope their work or fail closed.
- Fast SQLite tests and a real PostgreSQL 15 migration/concurrency suite pass.
- Registration and all paths to a second writable subject remain disabled.
