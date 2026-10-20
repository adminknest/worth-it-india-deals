# Worth It India Operator — agent specification

Version: 1.0  
Owner: Worth It India  
Primary pilot network: EarnKaro  
Primary destination: Telegram channel  
Mode: approval-first

## Mission

Find genuinely useful India shopping deals from permitted sources, verify the current merchant facts, create an authorized affiliate link, and place a small number of high-confidence posts into an owner-controlled review queue. Protect audience trust and confirmed commission before optimizing volume.

This agent is an operator around a deterministic pipeline. An AI model may help classify or rewrite text, but it is never the authority for price, stock, discount, merchant policy, link affiliation, approval or publication state.

## Non-negotiable invariants

1. No candidate enters review without an allowed source and stable product identity.
2. No deal is approved without current price-and-stock evidence and an allowlisted affiliate link.
3. Approval is bound to a material snapshot: source, merchant, product, title, URL, price, MRP and affiliate URL. Any change resets approval.
4. No publication occurs without a matching approval snapshot and fresh verification evidence.
5. A publication attempt is reserved before the Telegram call. Timeout or ambiguous server failure becomes `uncertain`, pauses publishing and is never retried automatically.
6. No retailer or affiliate platform is scraped or accessed through undocumented private endpoints.
7. No paid ads, brand bidding, fake scarcity, invented coupon, exaggerated savings or unsupported “lowest price” claim.
8. Every post discloses the affiliate relationship and says that price, stock and delivery may change.
9. Only the configured owner account may approve, reject, publish, pause or resume.
10. Tokens, OTPs, bank data and passwords never enter source code, deal records, logs or prompts.
11. Amazon posts name Amazon as the link destination, never use “loot” or “bug” framing, and their price content and evidence expire within 24 hours.

## Components and authority

| Component | May do | Must not do |
|---|---|---|
| Intake | Read permitted CSV/JSON, a current EarnKaro deal page, or an owner-entered URL | Crawl stores or copy unknown feeds |
| Normalizer | Validate schema, canonicalize URLs, create merchant/product identity | Guess a missing price or product ID |
| Verifier | Record merchant-page price, stock, timestamp and evidence note | Treat an affiliate headline as merchant verification |
| Policy gate | Apply network, merchant and destination rules | Override a merchant restriction automatically |
| Monetizer | Attach an EarnKaro Profit Link from the supported interface | Manufacture or modify tracking links |
| Ranker | Score verified candidates by discount, price band, evidence and commission | Publish solely because commission is high |
| Reviewer | Show snapshot and collect owner approval or rejection | Preserve approval after a material change |
| Publisher | Reserve, format and send one approved post | Retry an ambiguous Telegram send |
| Ledger | Import reports idempotently and separate pending, confirmed, rejected and paid profit | Count pending profit as earned cash |
| Incident controller | Pause, record the failure, expose diagnostics and require reconciliation | Hide or endlessly retry failures |

## State model

```text
candidate
   ├── rejected_source
   ├── rejected_quality
   └── verified
         ├── policy_blocked
         └── monetized
               └── pending_approval
                     ├── rejected_by_owner
                     └── approved(snapshot hash)
                           └── reserved
                                 ├── failed_definite
                                 ├── uncertain → global pause → manual reconciliation
                                 └── sent
```

`posted` is terminal for that exact product, price and affiliate-link snapshot. A later price creates a new review requirement rather than silently reusing the old approval.

## Tool contracts

### `ingest(source)`

- Accept only local owner-controlled files or remote hosts on the configured allowlist.
- Reject cross-host redirects, payloads over 5 MiB, non-array JSON and non-object entries.
- Preserve product identity and record material changes.

### `record_verified_deal(...)`

- Require merchant URL, EarnKaro source URL, Profit Link, exact price, MRP, stock status, observation time and evidence note.
- Require the source to be `earnkaro.com` and the Profit Link host to be allowlisted.
- Save evidence and leave approval pending.

### `approve(product_key, actor)`

- Require active status, an affiliate link, matching price evidence and in-stock evidence.
- Save the complete material fingerprint, actor and approval time.

### `publish_next()`

- Check the global pause, interval, daily cap, freshness, approval fingerprint and evidence.
- Reserve an idempotency key derived from destination, product, price and affiliate link.
- On timeout or HTTP 5xx, mark the outcome uncertain and pause.
- On success, persist the Telegram message ID before the next item is eligible.

### `reconcile(attempt, outcome)`

- Owner-only recovery tool.
- For `sent`, require the real Telegram message ID.
- For `failed`, release only that attempt for a deliberate future decision.
- Append the action to the audit chain.

## Scheduling

- Candidate scan: every 60 seconds when an authorized feed exists.
- Publish eligibility: every scan, constrained to one post per 30 minutes and 24 posts per UTC day.
- EarnKaro merchant verification and link conversion: browser-assisted during the pilot.
- Commission import: daily or whenever EarnKaro provides a new report/export.
- Health check: before start, after deployment and at least daily.
- Consistent database backup: daily while the worker is stopped or through SQLite's online backup command.

One worker lease prevents two publishers or two Telegram operators from running concurrently against the same database.

## Observability and incident policy

The database stores a hash-chained audit trail, publication attempts, operator state, incidents, verification evidence, approvals and commission events. Audit records contain actions and non-secret context; they never contain tokens or passwords.

Severity:

- `critical`: ambiguous send, invalid audit chain, database integrity failure or unauthorized operator behavior. Pause immediately.
- `warning`: definite Telegram rejection, stale evidence, feed validation error or commission import mismatch. Quarantine the item and continue safe work.
- `info`: normal ingest, verification, approval, rejection, publication and reconciliation.

Service objectives for the pilot:

- Zero duplicate Telegram posts from retries.
- Zero posts without current matching evidence and approval.
- Zero secrets in code or logs.
- Recovery from process restart without losing a sent-message record.
- Health diagnosis in one command.

No system can promise zero failures. This design aims to make failures visible, bounded and recoverable without misleading the audience.

## Evaluation suite

Release only when all checks pass:

1. Same product ingested twice produces one record.
2. Sold-out, stale, invalid-price and weak-evidence candidates are rejected.
3. Approval without evidence is blocked.
4. Price or affiliate-link change resets approval.
5. Duplicate or uncertain publication reservation cannot send again.
6. Commission report replay does not double count.
7. Merchant social restriction cannot be overridden by the model.
8. Database integrity, foreign keys and audit chain pass `doctor`.
9. A consistent backup opens and passes `PRAGMA integrity_check`.
10. One controlled Telegram post appears once, with correct disclosure and link.

## Scale triggers

Stay on Python plus SQLite while there is one worker and modest volume. Move to PostgreSQL and a managed job queue only when measured concurrency, retention or reporting needs exceed the single-node design. Add webhooks when there is a stable HTTPS service; Telegram documents long polling and webhooks as mutually exclusive, so migration must explicitly disable one before enabling the other.

Do not add an LLM, Redis, Kafka, Airflow, Temporal, Kubernetes or multiple autonomous agents merely to make the diagram look sophisticated. Each new service must remove a measured bottleneck or failure mode.
