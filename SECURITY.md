# Security Policy

## Supported Versions

This commercial fork is completing a multi-user transition. Only the latest
commit on `commercial/main` is supported; upstream `master` remains the
single-user self-hosted edition.

| Version | Supported |
|---------|-----------|
| latest (`commercial/main`) | ✅ |
| older commits | ❌ |

## Reporting a Vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

If you discover a security issue, email the maintainer privately:

📧 **ilodezzis@gmail.com** — subject line: `[Vitals] Security Vulnerability`

Include:
- A clear description of the issue
- Steps to reproduce
- Impact assessment (what data or access could be affected)
- Any suggested fix (optional but appreciated)

You will receive a response within **72 hours**. If the issue is confirmed, a fix will be released and you'll be credited in the commit message (unless you prefer to remain anonymous).

## Security Design Notes

The commercial branch is designed for a shared service with patient, professional,
support, and platform roles. Until the final release gate is recorded, deploy it
only in a controlled environment and keep public registration disabled. The
security model assumes:

- The application runs behind a TLS-terminating reverse proxy; operational
  interfaces remain private
- Only provisioned accounts can enter while registration is disabled
- The `.env` file with credentials is never committed to the repository

Key security controls already in place:
- **Bcrypt** password hashing
- **Signed session cookies** (itsdangerous)
- **CSRF protection** via Origin header validation
- **CSP headers**
- **Loopback-only port binding** in `docker-compose.yml` (`127.0.0.1:8000`)
- **MCP OAuth 2.0 + PKCE** for Claude.ai integration

### Commercial branch transition

The `commercial/*` branches are an in-progress multi-user rewrite. Public
registration remains disabled until subject ownership, scoped services, files,
integrations, sessions, MCP, and PostgreSQL RLS have all passed their isolation
gates. A schema row or role is not evidence that the branch is ready to host
multiple people.

The legacy environment-backed owner is materialized as an active database user,
`member`, `platform_superadmin`, and self-owned health subject during startup.
Startup fails closed if the configured username or bcrypt hash disagrees with a
non-empty identity database. The last active platform superadmin cannot be
revoked or suspended through identity services.

`platform_superadmin` is an operational role, not standing permission to inspect
health data. Patient data requires a short-lived, subject-bound support grant
with an explicit reason, approver, expiry, mode, and concrete scope. This is how
maintainers can investigate and repair production issues without creating an
invisible global medical-record bypass.

Browser cookies are signed, not encrypted. New compatibility cookies carry only
a format version, token type, legacy auth source, and username; roles, subjects,
grants, credentials, and PHI are deliberately excluded. Existing signed
bare-username cookies remain accepted only for their configured lifetime. The
current version marker prepares the later database-session cutover; it does not
yet make legacy cookies individually revocable.

The legacy password bridge writes `.env` and PostgreSQL as one logical change,
but no filesystem/database transaction can make them physically atomic. An
ordinary commit error or request cancellation restores the previous environment
hash. A hard process stop or ambiguous database commit can still leave the two
copies different; startup then deliberately refuses to choose one. Recovery is
an operator action: restore the intended bcrypt hash from a trusted secret backup
to both stores (without printing it to logs), bump `users.session_version`, and
restart. Until database sessions land, rotate `VITALS_SESSION_SECRET` as a
separate step when already issued browser/MCP credentials must be invalidated.
Never make bootstrap overwrite one side automatically.

## Inbound Endpoints

There is no webhook. `POST /tg/<path>` — the Telegram bot's endpoint, and the
only route in the app that was reachable without a session — has been removed
along with the bot, and so has its CSRF exemption: a prefix that waves through
requests to a route nobody has mounted is a door left open for whatever gets
mounted behind it next.

What remains reachable without a session is listed and enforced in
`tests/test_anonymous_surface.py`, which fails if a new route joins that set
without a stated reason. Today it is the login and OAuth handshakes, `/health`,
the Bearer-token external summary, and the published doctor document.

The authenticated account-notification API stores only the current browser's
Web Push subscription. A subscription belongs to an account/device, not to a
health subject: one doctor's browser can receive work across several separately
authorized patients without copying a delivery credential into every record.
The endpoint and browser encryption keys are encrypted under the installation's
credential key, never listed back to the browser, and erased on revocation or
account suspension. The separate care-message outbox is subject-scoped, stores
no rendered payload or medical text, and must revalidate live relationship and
consent before delivery.

## Revoking Claude.ai Access

Every newly issued MCP credential carries a `jti` backed by a durable registry
row. Disconnect that connector from Settings to revoke it without signing out
other browsers or connectors. Each token also names one health subject and an
exact capability set. A professional token is bound to a relationship and
consent version, so pausing, revoking, expiring, or replacing that consent takes
effect on the next verification.

Pre-registry owner credentials are adopted on first use and then become
individually revocable. Rotating `VITALS_SESSION_SECRET` remains an emergency
installation-wide action: it invalidates every signed browser and MCP token.

## What is NOT a Security Issue

- The application being accessible on your own local network
- Rate limits being bypassable by the single authorized user
- Log messages containing non-sensitive operational information
