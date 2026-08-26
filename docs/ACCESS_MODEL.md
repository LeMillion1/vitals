# Access model

This document is the normative vocabulary for Vitals identities, roles, and
operational authority. It applies to the commercial shared-service branch.
Historical migration documents may use *owner*, *operator*, or *administrator*
more loosely; when they conflict with this document, use the terms below.

## The two administrative boundaries

A **host operator** and an application **`platform_superadmin`** are not the same
capability.

- The host operator controls the server: SSH, the Compose project and production
  overlay, deploys, migrations, PostgreSQL owner credentials, runtime-file
  creation, backup replication, restore drills, and recovery. This is an
  infrastructure trust boundary, not an application role or an application
  authorization to read a health record. Because this operator can technically
  reach storage and backups outside Vitals, the deployment must nevertheless
  treat the operator as trusted with their confidentiality.
- `platform_superadmin` is an additive role on an active Vitals account. It
  authorizes reviewed application control-plane actions such as the Platform
  hub, OpenRouter configuration, account admission, professional review,
  support workflows, and the web-runtime restart action. It grants neither SSH
  access nor migration, database-owner, backup-repository, or Compose authority.
  It also grants no standing access to protected health information (PHI).

One person may hold both kinds of authority in a small installation, but every
operation must still be authorized at the boundary where it occurs. A browser
session cannot substitute for host access, and host access must not be presented
as an ordinary patient-record permission.

## Accounts, roles, and health subjects

An account answers **who is acting**. A health subject answers **whose health the
data describes**. Those identities must not be inferred from one another.

Roles are additive. A person may, for example, be both a `member` and a `doctor`,
or a `member` and a `platform_superadmin`. Adding roles does not create an
unscoped union of patient records: every protected operation resolves one exact
subject and one authorization basis.

| Identity or capability | What it authorizes | What it never authorizes by itself |
| --- | --- | --- |
| Host operator | Server, deploy, migration, runtime configuration, and disaster-recovery operations | A patient-record read, professional care, or an in-app support grant |
| Active `platform_superadmin` | Reviewed Vitals control-plane actions exposed by the Platform surfaces | Standing PHI access, SSH, Compose, migrations, database-owner credentials, or worker control |
| `member` with an owned health subject | The owner's subject-scoped product actions, settings, integrations, portability, and connector grants | Another subject's record or installation control-plane authority |
| `doctor` | Eligibility to submit a doctor profile and, after verification by a different `platform_superadmin`, to act through an exact doctor relationship | Any patient's PHI merely because the role or profile exists |
| `trainer` | Eligibility to submit a trainer profile and, after verification by a different `platform_superadmin`, to act through an exact trainer relationship | Any patient's PHI merely because the role or profile exists |
| Support grant | One named platform administrator, one exact subject, one separately approved mode, scope, and expiry | Other subjects, broader scopes, or access after decline, revocation, consumption, or expiry |
| Break-glass session | One holder, one exact subject, reviewed read-only summary domains, two independent approvals, and a 15-, 30-, or 60-minute window | Notes, care messages, plans, raw payloads, files, exports, repairs, writes, MCP, or an installation-wide patient directory |

`member` is a role, while record ownership is a durable relationship between a
user and a health subject. Ordinary member admission creates both together.
Doctor and trainer accounts remain without a personal record unless a host
operator explicitly creates one through the provisioning command; a
professional who also owns a record still needs a separate professional
authorization basis to open somebody else's record.

## Professional access

A `doctor` or `trainer` role is an identity attribute, not a PHI grant. Access to
another person's record requires all of the following at the time of use:

1. an active account with the exact professional role;
2. a verified professional profile of the same kind;
3. an active care relationship with the exact subject;
4. the current, unexpired, unrevoked consent version;
5. a consent scope that permits the requested domain and action.

Accepting a care invitation creates the relationship, not consent. The record
owner decides what to share afterward. Pausing or ending the relationship, or
replacing, expiring, or revoking consent, closes the corresponding professional
web, file, message, notification, and MCP access on its next authorization
check.

Care conversations are patient-visible. Read and write-message scopes are
separate; historical participation does not silently add a new professional to
an older conversation.

## Support and break-glass

Support is not a standing role grant. A platform administrator requests access
to one opaque record code, and the record owner approves or declines the exact
mode:

- record read;
- one-time personal export;
- one schema-fixed, reversible repair.

Each mode has its own grant and checks. A read grant cannot become an export or
repair grant. Successful access is time-bounded, subject-bound, scope-bound,
revalidated, and visible to the patient.

Break-glass is independent of both professional consent and ordinary support.
The requesting `platform_superadmin` gains nothing until two distinct other
active platform superadmins approve. The resulting session is a short,
read-only, fixed projection for one exact subject. The patient can see and
revoke pending or active access.

## Process and restart boundary

Production runs web and scheduler as separate services with distinct restricted
PostgreSQL logins.

- `vitals_app` serves the browser, MCP, and external HTTP surfaces.
- `vitals_worker` owns APScheduler and provider/background jobs and publishes no
  HTTP port.
- The Platform **Restart web runtime** action terminates only the web process.
  Compose may recreate that container according to its restart policy, but the
  action does not restart `vitals_worker`, run migrations, reconcile database
  roles, deploy an image, or restart the whole Compose project.
- Restarting or replacing web and worker together is a host-operator procedure.

The host/operator `.env` is never mounted into either runtime. Web mounts the
dedicated runtime configuration directory read/write so Settings can atomically
replace `vitals.env`; worker mounts the same directory read-only and receives its
own database URL through the reviewed Compose mapping.

## Authorization bases for PHI

Every permitted PHI operation uses exactly one explicit basis:

1. self-ownership of the selected health subject;
2. a professional relationship plus current consent;
3. a patient-approved support grant;
4. an independently approved break-glass session.

Application roles, host access, provider identity claims, possession of a row
identifier, and payment for the platform AI gateway are not additional PHI
authorization bases.
