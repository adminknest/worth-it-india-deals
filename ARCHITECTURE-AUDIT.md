# Pipeline architecture audit — 20 October 2026

## Decision

Keep the pilot on a single Python worker and SQLite. Harden the transactional boundaries now; defer distributed infrastructure until the business has enough verified deal volume and revenue to justify it.

The modern design principle applied here is not “more services.” It is explicit state, idempotency, bounded retries, evidence, auditability, least privilege and safe recovery.

## Findings and fixes

| Area | Previous risk | Implemented control |
|---|---|---|
| Approval | Approval survived price or affiliate-link changes | Approval is bound to a material SHA-256 snapshot; any material change resets it |
| Verification | A linked deal could be approved without merchant evidence | Approval now requires matching price and in-stock evidence |
| Duplicate sends | Reservation existed, but worker concurrency was uncontrolled | Publisher and operator leases allow one active instance of each |
| Ambiguous Telegram result | Timeout was paused, but server errors were treated as definite failures | Timeout and HTTP 5xx now become `uncertain`, create a critical incident and pause |
| Recovery | No supported way to resolve an uncertain attempt | Owner-only `reconcile` command records sent/failed outcome and message ID |
| Feed security | Allowlisted initial host could redirect elsewhere; response size was unlimited | Redirect host is revalidated, response is capped at 5 MiB, JSON shape is checked |
| Manual rejection | A fresh scan could reactivate an owner-rejected deal | Rejection persists for the same material snapshot and clears only after a real change |
| Repeat deals | No explicit repeat policy | Material reposts remain governed by a 72-hour cooldown setting |
| Accountability | State changes were spread across tables without one trail | Append-only hash-chained audit events record actor, action and safe context |
| Incidents | Errors were printed and could disappear | Incidents persist with severity, component and error class |
| Database recovery | WAL was enabled but no safe backup command existed | Online SQLite backup command plus integrity verification |
| Operations | Status was counts only | `doctor` checks integrity, foreign keys, audit chain, uncertain/stale attempts and approval consistency |
| Commission reporting | Report replay could double count | Stable event IDs and `INSERT OR IGNORE` make imports idempotent |

## Industry-aligned choices

Telegram officially documents that `getUpdates` and webhooks are mutually exclusive, and that offsets prevent duplicate update delivery. Long polling is appropriate for the no-cost single-worker pilot; a webhook becomes useful after a stable HTTPS service exists. Telegram also advises keeping a single chat below roughly one message per second and groups below 20 per minute. The pilot's one-post-per-30-minutes limit is far below those transport limits because audience quality, not API capacity, is the real constraint.

SQLite WAL is a strong single-node choice, but the WAL file is part of persistent state and must remain with the database during live copying. The code therefore uses Python's online SQLite backup API, which the Python documentation states works while other clients access the database.

OWASP's logging guidance distinguishes operational/audit trails from ordinary server logs and warns against logging secrets. The pipeline stores structured audit facts and incidents in SQLite while keeping tokens, passwords, OTPs and payment details out of records.

EarnKaro's current terms say customized links may be shared in personal networks, payment depends on a genuine tracked and confirmed transaction, and paid advertising/brand bidding or other retailer-rule violations can lead to termination and forfeited profit. That supports an organic Telegram pilot with merchant-specific checks; it does not support uncontrolled scraping or bulk spam.

Official references:

- [EarnKaro terms](https://earnkaro.com/terms-conditions)
- [EarnKaro FAQ](https://earnkaro.com/faq)
- [Telegram Bot API](https://core.telegram.org/bots/api)
- [Telegram Bots FAQ](https://core.telegram.org/bots/faq)
- [SQLite WAL documentation](https://www.sqlite.org/wal.html)
- [Python SQLite backup API](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup)
- [OWASP Logging Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html)

## What remains intentionally manual

- Register the owned Telegram channel as the EarnKaro traffic source.
- Verify a merchant page before each pilot deal.
- Generate the Profit Link through EarnKaro's supported interface.
- Approve or reject the exact snapshot.
- Reconcile ambiguous Telegram outcomes.
- Request payout and manage bank details directly in EarnKaro.

These steps are manual because the account currently exposes no documented publisher API for them, or because they are high-impact owner decisions. Automating them through private endpoints would make the system less reliable and could put the affiliate account at risk.

## Next upgrade triggers

| Trigger | Upgrade |
|---|---|
| Stable public HTTPS worker | Replace long polling with a verified Telegram webhook secret |
| More than one machine or sustained write contention | Move operational tables to PostgreSQL |
| Several independent slow integrations | Add a small durable queue and per-connector workers |
| Documented EarnKaro/Cuelinks API access | Replace browser-assisted link creation with the approved API |
| More than 100 live catalog pages | Add a generated index, sitemap, structured data and Pagefind |
| Measured duplicate-title misses | Add fuzzy matching after exact product-ID/URL dedupe |

No migration should occur merely because a larger stack is fashionable. Each change needs a measured trigger, rollback plan and passing end-to-end test.
