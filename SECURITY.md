# Security Policy

## Supported Versions

Vitals is a single-user, self-hosted application. Only the latest commit on `master` is supported.

| Version | Supported |
|---------|-----------|
| latest (`master`) | ✅ |
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

Vitals is designed for **single-user self-hosted deployment**, not as a multi-tenant SaaS. The security model assumes:

- The application runs on your own server behind a VPN or Cloudflare Access
- Only you have access to the dashboard
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

**When web push replaces the transport**, its subscription endpoint is the next
thing to appear here. The shape to keep: a per-subject subscription in the
database, not one credential in the environment — the reason the Telegram
transport could not survive a shared installation is that one bot token and one
chat id cannot belong to more than one person.

## Revoking Claude.ai Access

MCP access tokens are stateless (signed, not stored), so there is no per-token
revoke. To revoke Claude.ai's access, **rotate `VITALS_SESSION_SECRET`** in your
`.env` and restart the app. That invalidates every signature at once — all MCP
tokens *and* your browser session cookie — so you will need to log in again and
reconnect the Claude.ai connector.

## What is NOT a Security Issue

- The application being accessible on your own local network
- Rate limits being bypassable by the single authorized user
- Log messages containing non-sensitive operational information
