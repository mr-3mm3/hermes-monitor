# Changelog

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