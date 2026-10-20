# Worth It India pipeline audit — 21 October 2026

## Executive finding

The two-hour silence had two causes. First, all four approved deals had already been sent and the verified queue was empty. Second, Windows had left the scheduled worker queued because it was configured to avoid starting on battery power and to stop when the machine switched to battery. Both battery restrictions are now disabled, the worker is running, and a newly verified fifth deal was published as Telegram message 11.

The current bottleneck is ingestion. EarnKaro's supported Make Links interface converts one product URL at a time, and this account has no documented bulk publisher API attached to the worker. A one-minute publishing setting cannot produce one compliant post per minute unless a permitted source continuously supplies current products, merchant evidence and authorized tracking links.

## Observed state

| Check | Result |
|---|---|
| Posted deals | 5 |
| Fresh approved queue | 0 |
| Rejected candidates during recovery | 2 stale listings and 1 low-evidence listing |
| Uncertain Telegram sends | 0 |
| Database integrity | OK |
| Audit hash chain | Valid |
| Active worker | Yes |
| Continuous authorized feed | No |
| Confirmed commissions imported | None yet |
| Subscriber baseline | 2 |

Nine historical warning incidents were transient DNS, TLS or read-timeout failures. The worker recovered after them, but the records stayed open and made the incident list misleading.

## Fixes implemented

- Added a worker heartbeat and exposed it through `/status` and `doctor`.
- Added an owner-only queue-starvation alert after 60 empty minutes, rate-limited to once every six hours.
- Added the actual fresh publishable queue count, last post time, open incidents and heartbeat freshness to `doctor`.
- Automatically resolves transient operator warnings after the Telegram poll succeeds again.
- Added six-hour subscriber snapshots and an owner-only `/growth` report with subscribers, posts, fresh inventory and confirmed commission.
- Disabled the scheduled task's battery start/stop restrictions so a laptop power-state change cannot silently suspend operations.
- Removed repetitive Telegram offset entries from the audit log while preserving security- and publication-relevant events.
- Kept publication idempotency, evidence freshness, price matching, affiliate-host checks and duplicate protection intact.

## Remaining operational gaps

1. **No continuous source:** the worker needs either documented EarnKaro bulk access, approved Cuelinks API access, or a manually maintained permitted feed. Browser automation against private endpoints is not an acceptable substitute.
2. **Local availability:** the current operator depends on this Windows machine and session. Fly.io deployment remains prepared but has not been activated.
3. **Inventory buffer:** maintain at least 12–18 approved deals for the next publishing day. Each item still expires after the configured evidence window unless reverified.
4. **Growth attribution:** source-labeled Telegram invite links require the bot to receive invite-management permission. That permission has not been added.
5. **Conversion ledger:** EarnKaro reports must be imported before confirmed commission and conversion metrics become meaningful.

## Cadence decision

Keep discovery and eligibility checks frequent. For the public main channel, 12–20 strong posts per day is the recommended starting range. A 720-post/day firehose will bury the best products, make attribution noisy and encourage users to mute the channel. If a firehose later has demand, place it in a separate opt-in channel or on the website.

## Daily operating target

- 12–18 verified publishable items prepared before the first posting window.
- Zero uncertain sends and zero price/stock mismatches.
- Queue starvation below 30 minutes during the active day.
- One 9 p.m. IST digest and a predictable quiet period overnight.
- Daily subscriber, source, click, order and confirmed-commission review.

