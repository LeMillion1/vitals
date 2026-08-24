# Federated login: setting it up, and the order to do it in

Last reviewed: 2026-08-23

Vitals stops authenticating anybody once `VITALS_OIDC_ISSUER` is set. Until then
the password login works exactly as it always did, and none of the OIDC routes
exist — they answer 404. Setting that one variable is the cutover.

That is deliberate. It means the switch happens when there is somewhere to
switch *to*, rather than on the deploy that ships the code. Follow the order
below and there is no window in which you cannot reach your own record.

The cutover is hard: after it there is no password login, and no second factor
inside Vitals. Both move to the provider, which is where password hashing,
reset, recovery codes, TOTP, WebAuthn and rotation already live and are already
done properly.

## Before you start

You need four values from the provider, and one of them can only be read after
you have logged into it once. Work through this with the provider running and
Vitals still on password login.

```bash
docker compose --profile idp up -d
```

ZITADEL comes up on `127.0.0.1:8080`, bound to loopback for the same reason the
app is: this is the door to the health record and belongs behind the same VPN.
Its first-run console output includes the initial admin credentials — capture
them, they are printed once.

## 1. Create the application in ZITADEL

In the ZITADEL console, create a project and inside it a **Web** application
with authentication method **Code**, and:

- **Redirect URI**: exactly the URL you will put in `VITALS_OIDC_REDIRECT_URL`,
  which is your Vitals origin plus `/auth/callback`. It must match character for
  character; a trailing slash is a different URI.
- **PKCE**: on. Vitals refuses a provider that does not offer S256, so this is
  not optional.
- **Post-logout redirect**: your Vitals origin.

Copy the **client ID** and **client secret**.

## 2. Find your own subject

This is the value that binds the account you already have in Vitals to the
identity ZITADEL will vouch for. It is an opaque number, not your username or
your email address.

Log into the ZITADEL console as the user you intend to be, open **Users**, select
yourself, and copy the **ID**. In the token this arrives as `sub`.

Email is deliberately not used for this. A provider may let somebody claim an
address later, and a link made on that basis would hand over the whole record.

## 3. Configure Vitals, and cut over

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

Log in. The first login whose subject matches `VITALS_OIDC_BOOTSTRAP_SUBJECT`
binds your existing user to that provider identity, once, under the identity
governance lock. Every login after that finds the link and does not need the
variable — you can remove it, and should.

The issuer must be `https` unless it is `http://localhost`, which is allowed
only because a machine talking to itself cannot be intercepted.

## Adding another person while registration is closed

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

`session_service.revoke_all_sessions` bumps the user's `session_version`, which
invalidates every session that account holds anywhere — immediately, not when a
cookie expires. A cookie issued a minute ago and one issued last month stop
working together.

## The licence question

ZITADEL is AGPLv3. Running it beside Vitals over OIDC does not make Vitals
AGPL: separate processes, a standard protocol, no linking — the same reasoning
by which an AGPL database beside an application does not infect the
application.

Three things do change the analysis, and none of them is "ran it in compose":

1. **Modifying ZITADEL** and letting people reach the modified version over a
   network. Then you publish your ZITADEL changes — not Vitals.
2. **Distributing it** as part of a product you ship. A compose file pulling a
   public image is weaker than shipping a combined artifact, but it is still
   distribution territory and wants a lawyer rather than this paragraph.
3. **Linking its code** into your own binary. Not applicable to a Python
   application talking OIDC to a Go service.

ZITADEL sells a commercial licence, which is the usual answer to (2). Keycloak
under Apache 2.0 is the alternative with none of this, at the cost of a JVM and
noticeably more memory.

This is not legal advice.
