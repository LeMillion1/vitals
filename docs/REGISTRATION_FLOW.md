# Registration flow

Last reviewed: 2026-09-02

This document is the implementation and operations reference for public account
registration. The product journey and role model are visualized in
[`REGISTRATION_CJM.html`](REGISTRATION_CJM.html).

## Product contract

A visitor starts at `/login` and chooses either **Sign in** or **Create account**.
Creating an account asks one question before leaving Vitals:

- **Track my own health** creates a `member` account and exactly one owned
  `HealthSubject`, then returns to `/today`;
- **Work as a doctor** creates a recordless `doctor` account, then returns to
  `/care` to submit a professional profile;
- **Work as a trainer** creates a recordless `trainer` account, then returns to
  `/care` to submit a professional profile.

`platform_superadmin` is never a public choice. A professional role is only an
account capability: it does not verify a qualification and does not grant
access to any health subject. Professional access still requires a verified
profile, an invitation from the person receiving care, an active relationship,
and current scoped consent.

## Trust boundaries

ZITADEL proves the identity and owns passwords, recovery and authentication
factors. Vitals owns product roles, professional review, care relationships and
record consent. Vitals does not consume ZITADEL project roles for these product
decisions.

The role choice is persisted as a `RegistrationIntent` for 15 minutes. The row
contains only an opaque UUID, one whitelisted account kind and lifecycle
timestamps. The browser receives a separately signed cookie containing only the
UUID; no account kind, email, provider subject or credential is stored in that
cookie.

The OIDC handoff binds the same opaque UUID to the PKCE request. At callback,
Vitals requires the browser cookie and handoff to match, locks and re-reads the
intent, re-checks that public registration is open, and consumes the intent in
the same database transaction that provisions and links the account. Expired,
spent, missing, malformed and wrong-mode intents share one refusal and create no
account. A rollback restores an intent if later provisioning fails.

An already-linked provider identity always signs in to its existing account.
Presenting a new registration intent cannot add, replace or remove its roles,
and the intent remains unconsumed.

## Professional onboarding

Doctor and trainer accounts own no personal health record by default. Their
first `/care` screen contains one short form:

- the name shown to people in their care;
- a doctor licence/registration number, or a trainer certificate/qualification,
  when available.

The role determines the immutable professional kind; the form contains no role
field. Submission creates a pending profile. The browser shows pending,
returned, verified and suspended states without exposing a patient roster before
verification. Validation errors remain on the form and preserve entered values.

A platform administrator reviews profiles at
`/settings/platform/professionals`. Verification is independent from
registration and remains audited. A reviewer cannot approve their own profile.

## Operating the public door

The deployment must set `VITALS_REGISTRATION_UNLOCKED=1` once, and OIDC must be
fully configured. This is the host-level safety gate. After that one deployment
decision, a recently authenticated platform administrator can open or pause
public registration at `/settings/platform/registration`; routine operation
does not require a shell command or container restart.

Opening the Vitals door does not alter ZITADEL. The existing ZITADEL instance
must separately allow user self-registration and use the Vitals `/login` URL as
its default redirect. Keep organization registration disabled.

Pausing Vitals registration invalidates every still-pending intent because the
callback re-checks the effective mode. Existing accounts continue to sign in.

## Email limitation

Account creation can run in a controlled password-only beta while ZITADEL emits
`email_verified=false`. Such an account has no verified local mailbox and cannot
use address-bound care invitations. Do not weaken that boundary. Configure and
test SMTP plus mailbox verification before treating care invitations, password
recovery or public onboarding as production-ready beyond the controlled test
group.

## Verification checklist

- `/login` exposes sign-in and, only while registration is effectively open,
  the create-account path.
- `/register` works at phone and desktop widths in Russian and English.
- Member, doctor and trainer callbacks produce their exact account shapes.
- A doctor or trainer receives no `HealthSubject` and sees no patient data.
- Replay, expiry, cookie/handoff mismatch and a closed door fail without an
  account or session.
- A linked identity cannot change roles through registration.
- The administrator can open and pause registration in the UI with an audited
  actor.
- Professional profile validation is rendered as HTML with the form values
  preserved.
- Alembic upgrade and downgrade are verified against PostgreSQL 15.
- The final image is exercised in Docker Compose and in a clean browser session
  before deployment, then the same flows are smoke-tested on the deployed
  build.

## Reference production evidence — 2026-09-02

The reference production checkout is
`b5fb96c55bbbcb4dbe5dedb8c9151b796b925e1f`, PostgreSQL reports `0085 (head)`,
and web and worker run the same healthy immutable image. The registration gate
is unlocked and both stored and effective modes are `open`.

A read-only production smoke verified the public three-role form, the Vitals to
ZITADEL redirect, discovery, and the canonical issuer. It intentionally did not
create a synthetic professional account. Exact member, doctor, and trainer
mutations are verified by automated PostgreSQL, Compose, and browser gates; a
live disposable professional journey remains separate evidence.

The newest health and identity manifests have matching Cloudflare R2 replication
markers. The exact health bundle restored from `0084`, migrated to `0085`, and
passed the distinct-role and multi-subject RLS recovery gates in 45.751 seconds.
