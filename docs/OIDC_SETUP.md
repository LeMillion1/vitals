# Federated login: setting it up, and the order to do it in

Last reviewed: 2026-08-23

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

## Before you start

Before the provider's first start, generate three independent secrets and put
them in `.env`:

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
selected; its preflight refuses to start with a missing value or a master key
whose length is not 32 characters.

You then need four OIDC values from the provider, and the owner subject can only
be read after you have logged into it once. Work through this with the provider
running and Vitals still on password login.

```bash
docker compose --profile idp up -d
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
VITALS_OIDC_BOOTSTRAP_SUBJECT=...                   # the ID from step 2
```

Restart the app. From this point:

- `/login` redirects to the provider, keeping wherever you were going.
- The password and TOTP routes answer 404.
- `authenticate()` refuses before it reads the stored hash, so the bcrypt value
  still sitting in the column is not a second way in.
- Signing out clears the Vitals cookie and redirects through the provider's
  logout endpoint, so returning to Vitals does not silently reuse the old IdP
  browser session. If provider discovery is unavailable, local logout still
  succeeds and the failure is logged.

Controlled-support request, approval, refusal and revoke actions require an
authentication performed within the last fifteen minutes. A stale session is
redirected through `/auth/start?step_up=true`; Vitals sends `prompt=login` and
refuses the callback if the provider's `auth_time` is missing or too old. After
returning to the support page, submit the decision again.

Log in. The first login whose subject matches `VITALS_OIDC_BOOTSTRAP_SUBJECT`
binds your existing user to that provider identity, once, under the identity
governance lock. Every login after that finds the link and does not need the
variable — you can remove it, and should.

The issuer must be `https` unless it is `http://localhost`, which is allowed
only because a machine talking to itself cannot be intercepted.

## Adding another person while registration is closed

The recipient half of `invite_only` is implemented, but this release does not
yet expose supported operator issue, revoke, or delivery controls. Do not enable
that mode operationally until those controls and scheduled retention land. Its
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
audit payload, or OIDC handoff cookie.

Creating a local account and deciding which provider identity may enter it are
two explicit operator actions. Run both from a shell with
`VITALS_DATABASE_URL` set:

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

1. Bring the provider back. It is a container beside the app with its own
   database; `docker compose --profile idp up -d` and its volume are the whole
   of it.
2. If its data is gone, restore `vitals_idp_pgdata` from backup. The identity
   store is separate from the health store on purpose: they have different
   restore rhythms, and one dump holding both invites restoring both when you
   meant one.
3. If you must reach the record without the provider at all, unset
   `VITALS_OIDC_ISSUER` and restart. Password login returns. That is a break-
   glass action, not a mode: it re-opens a door that the cutover closed, and
   the environment variable is the only thing holding it shut.

## Revoking a session

`authentication.sessions.revoke_all_sessions` bumps the user's `session_version`. The
shared web authentication boundary compares every federated cookie with that
live row, so every session the account holds stops at its next request rather
than waiting for cookie expiry. A cookie issued a minute ago and one issued last
month stop working together.

## The licence question

The image is pinned to ZITADEL `v2.66.0`, whose exact tagged source carries the
[Apache License 2.0](https://github.com/zitadel/zitadel/blob/v2.66.0/LICENSE).
The previous version of this runbook incorrectly described that tag as AGPLv3.
Do not copy either statement forward when changing the image: inspect the
licence and notices on the exact replacement tag, then review distribution and
attribution obligations for that version. This is not legal advice.
