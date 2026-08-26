# Federated login: setting it up, and the order to do it in

Last reviewed: 2026-08-26

Vitals stops authenticating passwords once the complete OIDC group is set:
`VITALS_OIDC_ISSUER`, `VITALS_OIDC_CLIENT_ID`,
`VITALS_OIDC_CLIENT_SECRET`, and `VITALS_OIDC_REDIRECT_URL`. Until then the
password login works exactly as it always did and the OIDC routes answer 404.
Setting only part of the group fails application startup; it never silently
leaves the password door open.

That is deliberate. It means the switch happens when there is somewhere to
switch *to*, rather than on the deploy that ships the code. Follow the order
below and there is no window in which you cannot reach your own record.

The cutover is hard: after it there is no password login, and no second factor
inside Vitals. Both move to the provider, which is where password hashing,
reset, recovery codes, TOTP, WebAuthn and rotation already live and are already
done properly.

OIDC startup does not require `VITALS_AUTH_USERNAME` or
`VITALS_AUTH_PASSWORD_HASH`. Before the first federated identity is linked, it
instead fails closed unless the database has exactly one active owner with the
member and platform-superadmin roles, exactly that owner's one health subject,
and an explicit `VITALS_OIDC_BOOTSTRAP_SUBJECT`. After the first owner login,
startup requires an active platform administrator with an owned subject bound
to the configured issuer. A typo or unplanned provider switch therefore cannot
start a process that has no usable recovery administrator.

## Before you start

> [!CAUTION]
> Compose no longer contains a runnable ZITADEL default. The previously
> referenced `v2.66.0` tag was obsolete and fell within published vulnerable
> ranges, including the official
> [critical IDOR advisory](https://github.com/zitadel/zitadel/security/advisories/GHSA-f3gh-529w-v32x)
> and [MFA bypass advisory](https://github.com/zitadel/zitadel/security/advisories/GHSA-cfjq-28r2-4jv5).
> First select a supported provider/version under its documented
> [lifecycle](https://github.com/zitadel/zitadel/discussions/9417), review the
> exact release's licence, independently verify its manifest digest, and land a
> reviewed code change that replaces the sentinel and unconditional preflight
> refusal with exact image digest(s) plus compatible configuration. An env
> override is deliberately insufficient to approve identity infrastructure.

Before an approved provider's first start, generate three independent secrets
and put them in a separate owner-only `.env.idp`, copied from
`.env.idp.example`:

```bash
# Exactly 32 characters. Back it up: changing it later loses access to data
# ZITADEL encrypted with it.
python -c "import secrets; print(secrets.token_hex(16))"
# Database password.
python -c "import secrets; print(secrets.token_urlsafe(32))"
# First administrator password; the prefix guarantees the required classes.
python -c "import secrets; print('Aa1!'+secrets.token_urlsafe(24))"
```

Store the results as `VITALS_IDP_MASTERKEY`, `VITALS_IDP_DB_PASSWORD`, and
`VITALS_IDP_ADMIN_PASSWORD`. These are required only when the `idp` profile is
selected; its preflight refuses a missing secret or a master key whose length
is not 32 characters. In the current release it then refuses unconditionally,
even when all three are valid, because no provider image/configuration has been
approved in version control yet.

Never put these three values in either the host/operator `.env` or the
application runtime file. Compose does not mount the operator `.env` into web or
worker: they receive only the allowlisted `.vitals-runtime/vitals.env`, with web
mounting its directory read/write and worker mounting it read-only. `.env.idp`
is passed only to explicit provider-profile commands and is never mounted into
the Vitals runtimes. The separate host-operator and application-admin boundaries
are defined in [`ACCESS_MODEL.md`](ACCESS_MODEL.md).

The profile also starts `vitals_idp_backup`, which writes a separate verified
PostgreSQL recovery stream below `backups/idp/`. Before the first production
start, follow the identity-provider gate in `BACKUP_RESTORE_RUNBOOK.md` against
a disposable ZITADEL database. Confirm that the production Compose override
mounts the same protected host backup directory into this sidecar and the
offsite sidecar. Shipping the script is not proof that its restore works.

For the current production checkout, every provider command must preserve its
project and production overlay:

```bash
test -f docker-compose.production.yml
export COMPOSE_PROJECT_NAME=vitals_prod
export COMPOSE_FILE=docker-compose.yml:docker-compose.production.yml
```

That overlay is an untracked, owner-only production file. Before startup, render
the config and prove that both IDP sidecars use `/root/vitals/backups`, not the
checkout-relative base path.

You then need four OIDC values from the provider, and the owner subject can only
be read after you have logged into it once. Work through this with the provider
running and Vitals still on password login.

```bash
docker compose --env-file .env --env-file .env.idp \
  --profile idp up -d --wait vitals_idp_backup
```

ZITADEL comes up on `127.0.0.1:8080`, bound to loopback for the same reason the
app is: this is the door to the health record and belongs behind the same VPN.
Sign in with `zitadel-admin@zitadel.<external-domain>` and the value of
`VITALS_IDP_ADMIN_PASSWORD`, then complete the required password change. The
pinned image's upstream default is publicly known; the Compose preflight and
explicit first-instance password exist to ensure it is never used. Changing a
`FIRSTINSTANCE` value after `vitals_idp_pgdata` has been created does not update
the existing administrator.

## 1. Keep provider registration closed

Vitals does not create a local account from an arbitrary provider identity.
Every person is provisioned and linked explicitly, so the provider must not
advertise self-registration. The Compose profile sets ZITADEL's default
instance login policy to `allow_register=false` for a new identity database.

That default-instance setting is only consumed while the instance is created.
If `vitals_idp_pgdata` already existed before this setting was added, open the
ZITADEL Console's instance login settings, disable user registration, and save
the policy. Do not delete the volume to make the default apply: it contains the
identity store. Verify the result in a private browser window: the login page
must not offer a register/sign-up action.

An identity accidentally created through provider self-registration still has
no Vitals account and cannot enter a health record. Remove or retain it under
your identity-retention policy; never bind it merely because its email matches.

Before issuing a Vitals registration link, an operator must also create or
invite that same person in the provider and give them a verified recovery/login
path. The current Vitals invitation proves local admission; it does not create
a ZITADEL user or send provider mail. Keep public registration disabled until
that provider-side onboarding step is implemented or operationally documented.

## 2. Create the application in ZITADEL

In the ZITADEL console, create a project and inside it a **Web** application
with authentication method **Code**, and:

- **Redirect URI**: exactly the URL you will put in `VITALS_OIDC_REDIRECT_URL`,
  which is your Vitals origin plus `/auth/callback`. It must match character for
  character; a trailing slash is a different URI.
- **PKCE**: on. Vitals refuses a provider that does not offer S256, so this is
  not optional.
- **Post-logout redirect**: your Vitals origin, including the trailing slash
  (for example `https://vitals.example.com/`). Vitals sends this exact URI with
  its client ID to the provider's discovered `end_session_endpoint`.

Copy the **client ID** and **client secret**.

## 3. Find your own subject

This is the value that binds the account you already have in Vitals to the
identity ZITADEL will vouch for. It is an opaque number, not your username or
your email address.

Log into the ZITADEL console as the user you intend to be, open **Users**, select
yourself, and copy the **ID**. In the token this arrives as `sub`.

Email is deliberately not used for this. A provider may let somebody claim an
address later, and a link made on that basis would hand over the whole record.

## 4. Configure Vitals, and cut over

```bash
VITALS_OIDC_ISSUER=https://idp.example.com          # no trailing slash
VITALS_OIDC_CLIENT_ID=...
VITALS_OIDC_CLIENT_SECRET=...
VITALS_OIDC_REDIRECT_URL=https://vitals.example.com/auth/callback
VITALS_OIDC_BOOTSTRAP_SUBJECT=...                   # the ID from step 3
```

Restart the app. From this point:

- `/login` redirects to the provider, keeping wherever you were going.
- Every pre-cutover password-session cookie is rejected even though its old
  signature remains valid, so it cannot skip the first provider binding.
- The password and TOTP routes answer 404.
- `authenticate()` refuses before it reads the stored hash, so the bcrypt value
  still sitting in the column is not a second way in.
- Signing out clears the Vitals cookie and redirects through the provider's
  logout endpoint, so returning to Vitals does not silently reuse the old IdP
  browser session. If provider discovery is unavailable, local logout still
  succeeds and the failure is logged.

Controlled-support and registration-invitation mutations require an
authentication performed within the last fifteen minutes. A stale session is
redirected through `/auth/start?step_up=true`; Vitals sends `prompt=login` and
refuses the callback if the provider's `auth_time` is missing or too old. After
returning to the support page, submit the decision again.

Log in. The first login whose subject matches `VITALS_OIDC_BOOTSTRAP_SUBJECT`
binds your existing user to that provider identity, once, under the identity
governance lock. Every login after that finds the link and does not need the
variable — you can remove it, and should.

The issuer must be `https` unless it is `http://localhost`, which is allowed
only because a machine talking to itself cannot be intercepted. Startup rejects
userinfo, invalid ports, query/fragment components, ambiguous paths, deceptive
`localhost` suffixes, and any callback that is not the exact
`VITALS_PUBLIC_URL` origin plus `/auth/callback`.

## Adding another person by invitation

The deployment gate must be persisted in the owner-only application runtime
file. A shell `export` affects only that one CLI process and does not change an
already-running web service. Stop web first so its Settings writer cannot race
the host-operator update, persist and read back the gate, then recreate and
health-check web before changing the stored mode:

```bash
docker compose stop vitals_app
python scripts/registration_gate.py --set unlocked \
  --confirm 'WEB STOPPED; UNLOCK REGISTRATION'
docker compose up -d --force-recreate --wait vitals_app
python scripts/registration_mode.py --set invite_only \
  --runtime-env .vitals-runtime/vitals.env \
  --confirm-web-recreated \
  'WEB RECREATED WITH REGISTRATION GATE ENABLED'
```

Do not proceed unless `registration_gate.py` reports
`"readback": "unlocked"` and Compose reports the recreated web service healthy.

After a fresh platform-superadmin login, open
`/settings/platform/registration`. Enter the exact recipient address and choose
Member, Doctor, or Trainer. Copy the returned link immediately: Vitals stores
only its digest and cannot show it again. The same screen lists live invitations
with masked addresses and can revoke them even after the mode is closed. Its
fixed link contract is:

```text
https://vitals.example.com/register/invite#token=<invitation bearer>
```

The nonce-authorized scrubber removes the fragment before any other application
script or stylesheet loads. Vitals exchanges it for an opaque, short-lived
HttpOnly browser claim, clears any previous local session on that device, and
forces a fresh provider login. Exchange is deliberately retryable until the
first successful callback, so a link scanner or lost cookie response cannot
strand the invite; final consumption is single-winner. Account creation still
requires the invitation to remain pending in `invite_only` mode and the provider
to return the exact invited address with `email_verified: true`. Neither the
bearer nor an email address belongs in a path, query parameter, application log,
audit payload, or OIDC handoff cookie. The shared scheduler expires overdue
proofs hourly and scrubs terminal applicant PII after 90 days. Its platform
failure alert and `/health` heartbeat make a stopped retention loop visible.

## Adding a member by administrator approval

Use the same stop, persisted-gate, recreate, and readback sequence with the
approval mode:

```bash
docker compose stop vitals_app
python scripts/registration_gate.py --set unlocked \
  --confirm 'WEB STOPPED; UNLOCK REGISTRATION'
docker compose up -d --force-recreate --wait vitals_app
python scripts/registration_mode.py --set admin_approved \
  --runtime-env .vitals-runtime/vitals.env \
  --confirm-web-recreated \
  'WEB RECREATED WITH REGISTRATION GATE ENABLED'
```

An unknown person may now complete the normal provider login. Vitals requires
the provider's exact verified address, creates one expiring member request, and
redirects through a one-time signed handle to a clean standalone waiting page
with an opaque reference. OAuth callback parameters are not retained in the
address bar, and the handle is cleared before any later login. Vitals does not
create a user, health record, role, or browser session at this point.

A freshly authenticated platform superadmin reviews the masked queue at
`/settings/platform/registration`. Approving a request atomically creates the
member account and provider binding; the person signs in again afterward.
Approval is refused if the deployment gate or mode closes, OIDC becomes
unavailable, or the configured issuer changed. Requests from a previous issuer
remain visible only so an operator can reject them with a private decision
note. Rejection is also available after closure, so stale applicant PII need
not remain pending until expiry.

After onboarding, close the stored mode before locking the deployment gate:

```bash
python scripts/registration_mode.py --set disabled \
  --runtime-env .vitals-runtime/vitals.env
docker compose stop vitals_app
python scripts/registration_gate.py --set locked \
  --confirm 'WEB STOPPED; LOCK REGISTRATION'
docker compose up -d --force-recreate --wait vitals_app
```

For a professional account provisioned directly by an operator, creating the
local account and deciding which provider identity may enter it are two explicit
actions. This is separate from the invite-only and admin-approved browser flows
above. Run both from a shell with `VITALS_DATABASE_URL` set:

```bash
python scripts/provision_account.py --username dr-ivanova --role doctor
python scripts/link_identity.py --username dr-ivanova \
  --issuer https://idp.example.com --subject 2417...
```

Use the provider's exact `iss` and opaque `sub` values. Never substitute an
email address: ownership of an address can change, while this link grants
access to health records. The link command refuses inactive or unknown local
accounts and never moves an identity already bound to somebody else.

## If a login is refused

Every refusal renders the same page on purpose: "no such account", "your account
is suspended" and "that token was not for us" are three sentences and one fact
to whoever is trying. The reason is in the application log, prefixed
`federated login refused:`.

The ones you are most likely to hit while setting this up:

| Log says | What to check |
| --- | --- |
| `provider metadata describes a different issuer` | `VITALS_OIDC_ISSUER` does not match what the provider publishes. Trailing slash, or http vs https. |
| `token endpoint refused the code` | Client secret, or a redirect URI that does not match character for character. |
| `no account on this installation` | The bootstrap subject is wrong, already spent, or the variable is unset. |
| `callback state does not match` | The handoff cookie was lost. Usually a proxy stripping cookies, or more than ten minutes between starting and finishing. |

## Getting back in if the provider is down

There is no password fallback — that was the point of a hard cutover. What there
is:

1. Bring the provider back with the same project, overlay, two env files, and
   approved image digest used at cutover. Its identity volume is independent.
2. If its data is gone, verify an exact `zitadel_bundle_<timestamp>.sha256`,
   restore its `zitadel_<timestamp>.sql.gz` into a new empty PostgreSQL 15
   database/new volume with `ON_ERROR_STOP`, then run the provider readiness,
   discovery, restart, and login checks from the restore runbook. The artifact
   is a logical SQL dump, not a copy of `vitals_idp_pgdata`.
3. If you must reach the record without the provider at all, first restore the
   exact trusted `VITALS_AUTH_USERNAME` and `VITALS_AUTH_PASSWORD_HASH` pair
   consistent with the database. In the same owner-only configuration change,
   remove `VITALS_OIDC_ISSUER`, `VITALS_OIDC_CLIENT_ID`,
   `VITALS_OIDC_CLIENT_SECRET`, `VITALS_OIDC_REDIRECT_URL`, and
   `VITALS_OIDC_BOOTSTRAP_SUBJECT`, rotate `VITALS_SESSION_SECRET`, and restart
   only the app. Removing just one OIDC variable is an invalid partial
   configuration and intentionally fails startup. This is a break-glass action,
   not a steady mode: it re-opens the password door and invalidates browser and
   MCP credentials, which must then be reconnected.

Keep the legacy username/hash in an owner-only recovery store through the
cutover observation window; password mode needs the exact pair consistent with
the database for that break-glass rollback. Once the owner binding and an IDP
restore have both been rehearsed, the running OIDC application may omit them.
A singleton legacy MCP token remains attributable from the database, but it
must be reconnected with an explicit account/subject binding before a second
health subject is added; otherwise it deliberately fails closed.

## Revoking a session

`authentication.sessions.revoke_all_sessions` bumps the user's `session_version`. The
shared web authentication boundary compares every federated cookie with that
live row, so every session the account holds stops at its next request rather
than waiting for cookie expiry. A cookie issued a minute ago and one issued last
month stop working together.

## The provider/version/licence gate

Compose contains no runnable provider image: it has a version-controlled,
nonexistent all-zero sentinel and an unconditional preflight refusal. Selecting
the profile therefore cannot start an old tag, and changing an env file cannot
approve a different binary. Current ZITADEL lines changed runtime/login
behavior and licence posture. Select the provider and version explicitly,
inspect that exact release and notices, prove that its tag resolves to the
recorded OCI digest, and review the matching Compose/login/backup configuration
before repeating the whole conformance and restore drill. This is not legal
advice.

For ZITADEL v4, approval is not a one-line image replacement. The reviewed
deployment must pin separate API and Login V2 images, preserve or reproducibly
recreate the login-client PAT/bootstrap state during disaster recovery, separate
the public HTTPS port from the loopback origin port, and split one-shot database
initialization/setup privileges from the non-superuser long-running role. The
Cloudflare route must also be tested for the provider's HTTP/2, h2c, and
gRPC-Web requirements. Keep the sentinel in place until those pieces and their
restore/restart tests land together.
