# Changelog

## 0.3.5

- Security: quota reads now resolve the active Hermes profile as their own home and use immutable credential snapshots for every profile, including the active profile. The root home is still included as a read-only sibling when a named profile is active, preventing expired root credentials from being refreshed or copied into the active profile's `auth.json`.

## 0.3.3

- Security: quota aggregation is now strictly read-only for non-active profiles. The old loader installed a sibling profile's home and secret scope only while loading its credential pool, then handed back the live pool and called `peek()`/`select()` on it after the scope was reset — which could prune aged-out DEAD entries, refresh an expired Anthropic OAuth token, and persist the result into the wrong profile's `auth.json`. Sibling profiles are now read into an immutable snapshot inside their own scope and only usage requests are issued against it; an expired sibling token reports `n/a` instead of being refreshed. Only the backend's own profile refreshes its own token. The process-global `HERMES_HOME` env-swap fallback was removed in favour of the context-local home override.

## 0.3.2

- Fixed Claude quotas still showing `n/a` on the Desktop backend: the multiplexed serve fails closed on unscoped credential reads, so the pool loader now runs inside each profile's secret scope and can resolve the OAuth/Keychain token.
- Quotas now show only providers with a real credential (OAuth token, API key, or a resolved DeepSeek key). Unconfigured providers are hidden; a provider that has credentials but is momentarily in error stays visible with its status.

## 0.3.1

- Fixed Claude quotas stuck at `n/a` on the Desktop backend: a credential store shared by several profiles was queried once per profile, so the same accounts were fetched 8 times per refresh. Anthropic's usage endpoint answers those bursts with `429` (about three minutes of `Retry-After`) and every window fell back to `n/a`. Each credential is now fetched once per collection and the result is reused across profiles.

## 0.2.2

- Packaging/compliance for Hermes plugin publication: completed plugin.yaml metadata (author, license, repository, permissions), manifest schema, LICENSE file, install docs.

## 0.2.1

- Fixed Claude quotas showing `n/a` for the active credential-pool account (OAuth token handling).
- Quotas now dynamically enumerate every active Hermes provider instead of a hardcoded set (local-only providers are excluded).

## 0.2.0

- Quotas now reflect each provider's active credential-pool account.
- The footer identifies the active account, while the menu lists every pool account with its plan, usage percentages, and an active marker.
- DeepSeek quota details now report the non-secret credential `key_source`.