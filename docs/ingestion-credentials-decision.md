# Ingestion credential decision

## Status

**Decided for the initial synchronous ingestion phase.** Revisit the credential
type if browser/mobile SDK ingestion or other authenticated API access is added.

## Decision

Each ingestion credential belongs to one analytics project and grants only the
ability to submit events through the ingestion API. The project is derived from
the credential; producers cannot select another project by adding a project ID
to an event.

Credentials are intended for trusted product backends. A credential is a
Bearer secret with an indexed public lookup prefix and a random secret
component. We store a SHA-256 digest of the full credential, compare digests
with `hmac.compare_digest`, and reveal the secret only at creation or rotation.
Rotation creates a new credential and leaves the old credential active until
it is explicitly revoked. Revoked credentials cannot be reactivated.

The credential can require `properties.product` and/or `groups.account` to be
present. These rules are configurable rather than built in as HappyFox-only
platform assumptions. At present, `require_product` checks for a nonblank
string; it does not bind the credential to one specific product value.

## How this differs from PostHog

PostHog does not use one token for every kind of access. Its public event-capture
endpoints accept a project token and do not require API authentication. That
token identifies the destination for capture and is suitable for SDK setup.
PostHog uses personal API keys for private API access. Its newer project secret
API keys are project-scoped, user-less service credentials with explicit scopes
for server-to-server access to supported private API actions. These are separate
from the event-capture project token. See the [PostHog API overview](https://posthog.com/docs/api),
[personal API key documentation](https://posthog.com/docs/api/personal-api-keys),
and [project secret API key implementation guide](https://github.com/PostHog/posthog/blob/master/.agents/skills/adding-project-secret-api-key-auth/SKILL.md).

Our ingestion credential is closer in spirit to a narrow service credential
than to PostHog's public capture token, but it is simpler than PostHog's scoped
project secret API keys: it is accepted only by event-ingestion endpoints and
does not grant general API access. The codebase also uses a `Workspace` above a
shared `Project`: related products can send into one project, while separate
deployments and databases isolate test, staging, and production data.

## Why this fits the initial use case

- Initial producers are trusted product backends, so they can keep a secret
  safely. A public SDK token would not authenticate which backend sent an
  event.
- Multiple products share one analytics project for cross-product analysis.
  Separate credentials allow one integration to be rotated or revoked without
  replacing a shared project token used by every producer.
- The credential grants ingestion only. It cannot authorize catalog, query, or
  staff-management APIs, unlike a broad user API key.
- Credential-level validation can require fields used by the HappyFox pilot
  while leaving generic event validation free of hard-coded product rules.
- The design is small enough for synchronous PostgreSQL ingestion and can be
  changed independently from a future queue or analytical storage backend.

## Trade-offs and limits

- **Credential operations are our responsibility.** Operators must provision,
  distribute, protect, rotate, and revoke a secret for each integration.
- **This secret must not go in browser or mobile code.** Client-side SDK support
  would need a separate public-token design and appropriate abuse controls; it
  must not expose or reuse these credentials.
- **The key does not prove event semantics.** A producer holding a valid key
  can still submit incorrect event data. `require_product` only checks that a
  value exists; it does not enforce that a given key represents, for example,
  `helpdesk` rather than `workflows`.
- **Accepted events do not currently store the credential ID.** Separate keys
  provide independent lifecycle control, but not a durable per-event source
  audit trail. If that becomes a requirement, bind credentials to an expected
  source and persist trusted source metadata separately from producer-supplied
  properties.
- **Authentication adds database work.** A request looks up the credential by
  its indexed prefix and updates `last_used_at`. This is reasonable for the
  current phase; high-volume ingestion may need throttled usage updates or a
  cache/queue architecture.
- **It is not a drop-in PostHog SDK credential.** Server integrations need a
  small client that sends the Bearer credential. Browser/mobile SDKs would need
  another supported credential flow.

## Future direction

Keep server-side ingestion credentials narrow. If browser/mobile producers are
added, introduce a distinct public project token rather than weakening these
secrets. If non-ingestion APIs need service access, define scoped API
credentials for those APIs rather than expanding the ingestion credential's
permissions.

PostHog references checked on 2026-09-21:

- [Public capture endpoints and project token](https://posthog.com/docs/api)
- [PostHog capture token lookup code](https://github.com/PostHog/posthog/blob/master/posthog/api/utils.py)
- [PostHog team-to-project permission mapping](https://github.com/PostHog/posthog/blob/master/posthog/rbac/user_access_control.py)
