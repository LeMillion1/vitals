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

## The Telegram Webhook

The proactive layer adds the only endpoint that is reachable without a session:
`POST /tg/<path>`. It is guarded in layers, and it is **off unless you configure
it** — with no `VITALS_TELEGRAM_WEBHOOK_SECRET` the route fails closed with 401,
it is never open.

- The path segment itself is a random secret (`VITALS_TELEGRAM_WEBHOOK_PATH`), so
  the endpoint is not discoverable by crawling.
- Telegram's `X-Telegram-Bot-Api-Secret-Token` header must match
  `VITALS_TELEGRAM_WEBHOOK_SECRET`. Both comparisons use `compare_digest` — a
  plain `==` on a secret leaks its prefix through timing.
- A rate limit sits in front of the route (fail-open on Redis, as everywhere else).
- Only a private chat whose chat id **and sender id** match the configured positive
  user id is accepted. Group/supergroup ids are refused for both inbound capture
  and outbound PHI delivery. A foreign/non-private update gets a plain `200` and
  is discarded. Not a 403: a distinguishable answer tells a prober they found
  something, and Telegram would retry a non-200 for hours.
- The complete upstream update is durably claimed by subject and `update_id`
  before parsing or action handling. Retries cannot double-write; edits retain
  raw history while superseding the prior normalized facts. A failure before the
  durable claim returns a retryable `503`; after that claim, the recovery sweep
  can complete stored normalization or callback state without losing the input.
  The immediate reply/echo is still best-effort until the notification outbox
  cutover: a transport failure cannot safely be retried without a provider-side
  idempotency key because acceptance followed by a timeout is ambiguous.

Optionally narrow the path to Telegram's own subnets at your reverse proxy — that
is infrastructure, not application code.

**Revoking bot access:** clear the webhook at Telegram
(`curl -X POST "https://api.telegram.org/bot<TOKEN>/deleteWebhook"`), then rotate
or blank `VITALS_TELEGRAM_BOT_TOKEN` and `VITALS_TELEGRAM_WEBHOOK_SECRET` in
`.env` and restart. Switching the **Signals** module off in Settings silences all
outgoing messages immediately, without a deploy — it is the emergency switch, not
a revocation.

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
