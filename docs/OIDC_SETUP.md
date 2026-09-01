# Federated login: setting it up, and the order to do it in

Last reviewed: 2026-08-27

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
inside Vitals. Both move to the provider. Password hashing, reset, recovery
codes, TOTP, WebAuthn and rotation therefore become provider responsibilities;
do not cut over merely because the provider container is healthy. Prove the
configured production flows first.

OIDC startup does not require `VITALS_AUTH_USERNAME` or
`VITALS_AUTH_PASSWORD_HASH`. Before the first federated identity is linked, it
instead fails closed unless the database has exactly one active owner with the
member and platform-superadmin roles, exactly that owner's one health subject,
and an explicit `VITALS_OIDC_BOOTSTRAP_SUBJECT`. After the first owner login,
startup requires an active platform administrator bound to the configured
issuer. That administrator may deliberately be a recordless installation
operator; subject ownership is not platform authority. A typo or unplanned
provider switch therefore cannot start a process that has no usable recovery
administrator.

## Before you start

> [!CAUTION]
> Compose approves ZITADEL `v4.16.2` only at the two reviewed OCI index digests
> committed in `docker-compose.yml`; the Login V2 image is a separate process.
> Caddy `2.10.2` is pinned separately and has no Docker socket. A later version
> change is an infrastructure migration: verify new digests, run `setup`, take a
> pre-upgrade identity bundle, and repeat the destructive restore drill first.

Before the first start, create `.secrets/` with mode `0700`, generate five
independent one-line secret files with mode `0600`, and copy
`.env.idp.example` to owner-only `.env.idp`. The example names the expected
paths:

```bash
# Exactly 32 bytes and no trailing newline; escrow the same file off-host.
python -c "import secrets,sys; sys.stdout.write(secrets.token_hex(16))"
# Run separately for DB admin, DB service owner, and read-only backup role.
python -c "import secrets; print(secrets.token_urlsafe(32))"
# First human administrator password.
python -c "import secrets; print('Aa1.'+secrets.token_urlsafe(24))"
```

The files are selected through `VITALS_IDP_MASTERKEY_FILE`,
`VITALS_IDP_DB_ADMIN_PASSWORD_FILE`, `VITALS_IDP_DB_SERVICE_PASSWORD_FILE`,
`VITALS_IDP_DB_BACKUP_PASSWORD_FILE`, and `VITALS_IDP_ADMIN_PASSWORD_FILE`.
Preflight rejects missing, multiline, reused database, or non-URL-safe password
files and a master key whose content is not exactly 32 characters. The master
key is mounted directly only into fail-closed preflight and a networkless
one-shot staging job. Local Compose exposes file-backed secrets as host bind
mounts, so that job copies the root-owned host file into a provider-only volume
as mode `0400` for ZITADEL's uid 1000; setup and API mount only that derived
volume read-only. DB admin, Login, backup, gateway, Vitals web, and Vitals worker
cannot read either copy.

Before treating the provider as a production sign-in boundary, the human owner
must also complete the controls that cannot be inferred from a healthy
container:

- change the first-instance administrator password;
- enroll a strong MFA factor and escrow recovery codes outside the VPS;
- create and separately escrow an independent `IAM_OWNER` break-glass
  credential, preferably with a second trusted administrator;
- configure an active SMTP provider and sender domain with SPF, DKIM and DMARC;
- prove initialization, verification and password-reset mail on a disposable
  non-administrator account;
- verify in a private browser that provider self-registration is absent.

Changing `FirstInstance` or default SMTP environment values after the identity
database exists does not update that instance. Use the ZITADEL Console or its
administrative API, then test the persisted result.

Never put these values in either the host/operator `.env` or the
application runtime file. Compose does not mount the operator `.env` into web or
worker: they receive only the allowlisted `.vitals-runtime/vitals.env`, with web
mounting its directory read/write and worker mounting it read-only. `.env.idp`
is passed only to explicit provider-profile commands and is never mounted into
the Vitals runtimes. The separate host-operator and application-admin boundaries
are defined in [`ACCESS_MODEL.md`](ACCESS_MODEL.md).

The profile also starts `vitals_idp_backup`, which writes a separate verified
two-artifact recovery stream below `backups/idp/`: a PostgreSQL dump and the
matching Login V2 client PAT, with the manifest published last. Before production
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

The only published provider origin is the Caddy gateway at
`127.0.0.1:${VITALS_IDP_ORIGIN_PORT}`. It routes `/ui/v2/login` to Login V2 and
all other paths to the API over h2c. API, Login, and PostgreSQL publish no host
ports. The public `https://<VITALS_IDP_DOMAIN>` route on TCP 443 is a separate opt-in
profile and cutover gate. Do not use a Cloudflare Tunnel for this hostname:
create a normal DNS A/AAAA record to the host, allow inbound TCP 80/443, and
preserve HTTP/2 and gRPC. When Cloudflare proxies the record, use
**Full (strict)** TLS mode and enable HTTP/2 and gRPC.

After DNS resolves to the intended host, start only the public gateway and its
already-running dependencies:

```bash
docker compose --env-file .env --env-file .env.idp \
  --profile idp --profile idp-public up -d --wait \
  vitals_idp_public_gateway
```

The public profile owns ports 80/443 and its own persistent Caddy certificate
volume; it mounts no provider or application secrets. Do not start it beside
another reverse proxy that already owns those ports. Prove externally that the
discovered issuer is exactly `https://<VITALS_IDP_DOMAIN>` with the default port
omitted, that Console and Login V2 render, and that both gRPC-Web and native
`grpcurl` work through 443. A plain HTTP smoke is not that proof. The loopback
gateway remains available as the credential-free recovery/debug origin and does
not share certificate state. Every proxy and Login V2 request sends the same
canonical authority through `VITALS_IDP_PUBLIC_AUTHORITY`; for HTTPS on 443 that
value is the hostname without `:443`. Vitals compares the issuer verbatim, so
`VITALS_OIDC_ISSUER`, bootstrap links, and operator commands must use the exact
discovery value rather than adding or removing a port themselves.
The public and loopback gateways share a proxy-only network with Login/API;
neither can reach the identity database network.
Login V2 receives only the canonical `Host` and `X-Forwarded-Proto` overrides in
`CUSTOM_REQUEST_HEADERS`. It derives its ZITADEL instance/public-host headers
itself; adding those headers to the override list duplicates their values and
causes a valid domain to be rejected as `Instance.NotFound`.
The public gateway trusts forwarded client addresses only from the reviewed
Cloudflare network ranges. Recheck those ranges against
`https://www.cloudflare.com/ips/` during every gateway-image review. If the
Cloudflare proxy is the intended security boundary, also restrict origin
ingress to those ranges after certificate issuance and prove that a direct
origin request cannot bypass the proxy; the Caddy rule alone preserves audit
addresses but does not implement a host firewall.

Sign in with the configured `VITALS_IDP_ADMIN_USERNAME` (ZITADEL may suffix the
default organization domain) and the value of
the file selected by `VITALS_IDP_ADMIN_PASSWORD_FILE`, then complete the
required password change. The
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

### Controlled open-registration exception

An installation deliberately using Vitals `open` registration must configure
the existing ZITADEL instance login policy dynamically in Console; changing the
fresh-instance Compose default does not update the identity database. Enable
**User Registration allowed**, keep **Organization Registration allowed** off,
and set **Default Redirect URI** to the exact public Vitals origin plus
`/login` (for example, `https://vitals.example.com/login`). The default redirect
is a recovery path for a lost authorization-request context, not the OIDC
callback: it starts a new request, which lets an already-created identity return
through the normal PKCE callback and local admission checks. Without it, Login
V2 can leave a newly registered person on its standalone `signedin` page.

Do not enable email verification until an active SMTP provider has passed a
real delivery test. A temporary password-only beta may explicitly accept
unverified mailboxes, but it has no reliable password recovery or email-bound
admission proof; invitation and administrator-approval modes still require the
literal `email_verified=true` claim and therefore must remain unavailable.
Revisit this exception before advertising registration beyond a controlled
test group.

## 2. Create the application in ZITADEL

In the ZITADEL console, create a project named **Vitals**. Keep authorization,
project-access, and role assertions off: record authorization remains in
Vitals, not in provider project roles. Inside it create a **Web** application
named **Vitals Web** with response/grant type **Authorization Code**, and:

- **Redirect URI**: exactly the URL you will put in `VITALS_OIDC_REDIRECT_URL`,
  which is your Vitals origin plus `/auth/callback`. It must match character for
  character; a trailing slash is a different URI.
- **PKCE**: on. Vitals refuses a provider that does not offer S256, so this is
  not optional.
- **Client authentication**: **Client Secret POST**. Vitals sends the client ID,
  client secret, and PKCE verifier in the token-request body. Do not choose
  Basic or the public `None/PKCE` method; PKCE supplements confidential-client
  authentication rather than replacing it.
- **Post-logout redirect**: your Vitals origin, including the trailing slash
  (for example `https://vitals.example.com/`). Vitals sends this exact URI with
  its client ID to the provider's discovered `end_session_endpoint`.
- **User Info inside ID Token**: on. Vitals intentionally validates and reads
  `email`, `email_verified`, and `preferred_username` from the signed ID token;
  it does not make a second request to `userinfo`. This is required for later
  invitation and admission flows.
- **Development mode**: off; access-token and ID-token role assertions: off.

Copy the **client ID** and **client secret**.

The setup-generated Login V2 PAT is not an application-provisioning credential.
Its `IAM_LOGIN_CLIENT` role may read the first user's immutable ID, but it lacks
`project.create` and `project.app.write`. Never promote that long-lived PAT to
owner: Login V2 holds it in memory. Create this one application from the Console
after the first human administrator has changed the initial password; a fully
API-driven alternative would require a separate short-lived privileged service
account and therefore creates more recovery and revocation work, not less.

## 3. Find your own subject

This is the value that binds the account you already have in Vitals to the
identity ZITADEL will vouch for. It is an opaque number, not your username or
your email address.

Log into the ZITADEL console as the user you intend to be, open **Users**, select
yourself, and copy the **ID**. In the token this arrives as `sub`.

Email is deliberately not used for this. A provider may let somebody claim an
address later, and a link made on that basis would hand over the whole record.

## 4. Configure Vitals, and cut over

Do not edit four OIDC values into the live runtime file and then issue a broad
Compose restart. Use the host coordinator so the existing project, immutable
image, rendered application config, single Compose network, writable runtime
mount, exact non-shadowed container runtime controls, stopped-web boundary and
HTTP result are one crash-recoverable operation. The helper runs by attested
image ID under the host operator's UID/GID with only the runtime directory and
a temporary directory containing the two selected proof files; it does not
inherit medical-file, Garmin-session, backup, or sibling-secret mounts. The
private journal binds OIDC phases to a secret-safe keyed identifier of the
runtime provider group and rejects authority drift before recovery code runs.

Create two distinct files below an owner-only directory, each mode `0600` and
containing exactly one unpadded line:

- the ZITADEL Web application's client secret;
- the current plaintext Vitals legacy password, used only to prove that an
  automatic password rollback would actually be usable. Keep this proof until
  the observation window and identity restore/login test are complete.

From the exact live production checkout, define the non-secret values and the
fixed coordinator prefix. The state file is owner-only and contains no client
secret, password, subject, client ID, DSN, or subprocess output:

```bash
OIDC_ISSUER=https://idp.example.com                 # exact discovery value
OIDC_CLIENT_ID=...
OIDC_CLIENT_SECRET_FILE=/absolute/private/oidc-client-secret
LEGACY_PASSWORD_FILE=/absolute/private/legacy-password-proof
OIDC_REDIRECT=https://vitals.example.com/auth/callback
OIDC_OWNER_SUBJECT=...                              # opaque ID from step 3
export VITALS_IMAGE_TAG="$(git rev-parse HEAD)"     # exact running full SHA

OIDC_CUTOVER=(
  python3 scripts/oidc_cutover_host.py
  --project vitals_prod
  --compose-file "$PWD/docker-compose.yml"
  --compose-file "$PWD/docker-compose.production.yml"
  --env-file "$PWD/.env"
  --env-file "$PWD/.env.idp"
  --runtime-env "$PWD/.vitals-runtime/vitals.env"
  --state-file "$PWD/.vitals-oidc-cutover-state"
)

"${OIDC_CUTOVER[@]}" preflight \
  --issuer "$OIDC_ISSUER" \
  --client-id "$OIDC_CLIENT_ID" \
  --client-secret-file "$OIDC_CLIENT_SECRET_FILE" \
  --legacy-password-file "$LEGACY_PASSWORD_FILE" \
  --redirect-url "$OIDC_REDIRECT" \
  --bootstrap-subject "$OIDC_OWNER_SUBJECT"

"${OIDC_CUTOVER[@]}" cutover \
  --issuer "$OIDC_ISSUER" \
  --client-id "$OIDC_CLIENT_ID" \
  --client-secret-file "$OIDC_CLIENT_SECRET_FILE" \
  --legacy-password-file "$LEGACY_PASSWORD_FILE" \
  --redirect-url "$OIDC_REDIRECT" \
  --bootstrap-subject "$OIDC_OWNER_SUBJECT" \
  --confirm 'CUT OVER TO OIDC; AUTOMATIC ROLLBACK ON FAILED POSTFLIGHT'
```

The cutover rotates the installation session secret, recreates only
`vitals_app`, and leaves the journal at `awaiting_owner_binding`. Complete a
real owner login in the browser. A redirect smoke alone is insufficient: the
callback must exchange the code, create the exact provider binding and update
the durable owner's login time after this cutover's journal boundary. Only then
finalize:

```bash
"${OIDC_CUTOVER[@]}" finalize \
  --issuer "$OIDC_ISSUER" \
  --confirm 'OWNER OIDC LOGIN VERIFIED; FINALIZE CUTOVER'
```

Finalization refuses an identity binding or login left by an earlier attempt,
and again recreates only the web process. From this point:

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

During the bounded observation window, an explicit password rollback requires
the same still-owner-only plaintext proof and refuses if the database graph or
bcrypt hash changed:

```bash
"${OIDC_CUTOVER[@]}" rollback \
  --issuer "$OIDC_ISSUER" \
  --legacy-password-file "$LEGACY_PASSWORD_FILE" \
  --confirm 'ROLL BACK TO PASSWORD MODE AND ROTATE SESSIONS'
```

If the host command is interrupted, do not guess which phase completed. Run
`status`, then `recover`; supply `--issuer` for OIDC state and the legacy proof
when the journal says password compensation may be required. Recovery refuses
an incomplete phase if the image ID, rendered service config, or Docker network
changed since that phase.

After a successful identity restore drill, complete another fresh owner OIDC
login after the `oidc_bound` journal time. Only then may the operator run
`retire-legacy`. It first checks that login while web remains online, then
transactionally removes the durable owner's bcrypt verifier, records
`identity.password.retired`, clears both runtime bridge values, rotates the
installation session secret, and recreates only web. The normal password
rollback is intentionally impossible afterward:

```bash
"${OIDC_CUTOVER[@]}" retire-legacy \
  --issuer "$OIDC_ISSUER" \
  --confirm 'IDP RESTORE VERIFIED; RETIRE LEGACY PASSWORD'
```

The issuer must be `https` unless it is `http://localhost`, which is allowed
only because a machine talking to itself cannot be intercepted. Startup rejects
userinfo, invalid ports, query/fragment components, ambiguous paths, deceptive
`localhost` suffixes, and any callback that is not the exact
`VITALS_PUBLIC_URL` origin plus `/auth/callback`.

## Separating the platform operator from the health owner

After finalization, create a distinct provider identity for the installation
operator and copy its opaque `sub`. The narrow host command atomically creates
an active locked-password account with exactly the platform role, no health
record, and that exact provider binding. It refuses before the original owner
has a binding for the same issuer. Run it from the exact production Compose
project and image:

```bash
export VITALS_IMAGE_TAG="$(sed -n 's/^current_sha=//p' .vitals-deploy-state)"
test -n "$VITALS_IMAGE_TAG"
docker compose --env-file .env --env-file .env.idp \
  run --rm --no-deps vitals_migrate \
  python scripts/manage_platform_admin.py provision \
    --actor-username <current-owner-username> \
    --username platform-operator \
    --issuer https://idp.example.com \
    --subject <opaque-provider-subject> \
    --confirm 'PROVISION RECORDLESS PLATFORM OPERATOR'
```

Complete a fresh provider login as `platform-operator` and prove that `/`
lands on `/settings/platform`, then sign out and back in as the health owner.
Only after that proof may the new operator remove installation control from the
health account:

```bash
docker compose --env-file .env --env-file .env.idp \
  run --rm --no-deps vitals_migrate \
  python scripts/manage_platform_admin.py revoke \
    --actor-username platform-operator \
    --target-username <current-owner-username> \
    --issuer https://idp.example.com \
    --confirm 'REVOKE PLATFORM ADMIN ROLE'
```

The command verifies that the actor has the exact recordless operator shape and
a successful login through that issuer after its local identity was linked. The
last-active-platform-admin invariant also makes it fail closed if the new
operator is not active. No container restart is required for a role change:
navigation and every control-plane authorization re-read the durable role. The
health owner retains the `member` role and their record, but no longer sees or
may invoke container restart, OpenRouter, registration, professional
verification, or support controls.

## Adding another person by invitation

The deployment gate must be persisted in the owner-only application runtime
file. A shell `export` affects only that one CLI process and does not change an
already-running web service. Stop web first so its Settings writer cannot race
the host-operator update, persist and read back the gate, then recreate and
health-check web before changing the stored mode:

```bash
export VITALS_IMAGE_TAG="$(git rev-parse HEAD)"
docker compose stop vitals_app
docker compose run --rm --no-deps \
  -v "$PWD/.vitals-runtime:/run/vitals-runtime:rw" \
  vitals_app python scripts/registration_gate.py \
  --runtime-env /run/vitals-runtime/vitals.env --set unlocked \
  --confirm 'WEB STOPPED; UNLOCK REGISTRATION'
docker compose up -d --no-deps --force-recreate --wait vitals_app
docker compose exec -T vitals_app python scripts/registration_mode.py \
  --set invite_only --runtime-env /run/vitals-runtime/vitals.env \
  --confirm-web-recreated \
  'WEB RECREATED WITH REGISTRATION GATE ENABLED'
```

In production, never omit `VITALS_IMAGE_TAG`: Compose otherwise falls back to
the mutable `local` tag when it recreates web. The registration CLIs run in the
same immutable application image because the host is not required to have
SQLAlchemy or the rest of the application environment installed.

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
export VITALS_IMAGE_TAG="$(git rev-parse HEAD)"
docker compose stop vitals_app
docker compose run --rm --no-deps \
  -v "$PWD/.vitals-runtime:/run/vitals-runtime:rw" \
  vitals_app python scripts/registration_gate.py \
  --runtime-env /run/vitals-runtime/vitals.env --set unlocked \
  --confirm 'WEB STOPPED; UNLOCK REGISTRATION'
docker compose up -d --no-deps --force-recreate --wait vitals_app
docker compose exec -T vitals_app python scripts/registration_mode.py \
  --set admin_approved --runtime-env /run/vitals-runtime/vitals.env \
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
export VITALS_IMAGE_TAG="$(git rev-parse HEAD)"
docker compose exec -T vitals_app python scripts/registration_mode.py \
  --set disabled --runtime-env /run/vitals-runtime/vitals.env
docker compose stop vitals_app
docker compose run --rm --no-deps \
  -v "$PWD/.vitals-runtime:/run/vitals-runtime:rw" \
  vitals_app python scripts/registration_gate.py \
  --runtime-env /run/vitals-runtime/vitals.env --set locked \
  --confirm 'WEB STOPPED; LOCK REGISTRATION'
docker compose up -d --no-deps --force-recreate --wait vitals_app
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
   use the `idp-restore` profile from the restore runbook against a fresh
   project, and then run provider readiness, discovery, restart, and login
   checks. The recovery point is the logical SQL dump plus its exact Login PAT,
   not a copy of `vitals_idp_pgdata`.
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

The approved deployment pins ZITADEL API and Login V2 `v4.16.2` plus Caddy
`2.10.2` by OCI index digest. Environment files cannot replace those images.
It separates database provisioning, `init schema`, versioned `setup`, runtime,
Login, gateway, and backup; the long-running API uses only the non-superuser
database owner, while backup uses `pg_read_all_data` in the dedicated identity
cluster. The setup-generated Login PAT is part of every identity bundle.

Approval of the images is not approval to cut over production. Before adding
the four Vitals OIDC values, complete the tagged-image Compose conformance,
destructive DB+PAT restore and restart, independent offsite restore, and public
HTTP/2/gRPC/browser checks. Review the exact release notices and licence again
for every version change. This is not legal advice.
