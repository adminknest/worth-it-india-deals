# Production deployment

The repository uses GitHub Actions for CI and Fly.io for the continuously running Telegram worker. GitHub Pages cannot run this Python process, and GitHub Actions is not a persistent host.

## Required secrets

Store these only in Fly.io or GitHub encrypted secrets:

- `TG_BOT_TOKEN`
- `TG_CHANNEL_ID`
- `TG_ADMIN_USER_ID`
- `FLY_API_TOKEN` in the GitHub `production` environment

## One-time Fly.io setup

1. Create the `worth-it-india-deals` application in the `bom` region.
2. Create a 1 GB volume named `worth_it_india_data` in the same region.
3. Set the three Telegram secrets above.
4. Create a scoped Fly deploy token and save it in the GitHub `production` environment as `FLY_API_TOKEN`.
5. Run the deployment workflow.

The worker uses the 256 MB shared CPU configuration and mounts SQLite at `/data/deals.db`. Do not scale beyond one machine while using this SQLite volume.

## Owner workflow in Telegram

`/record` accepts one JSON object containing:

```json
{
  "merchant": "Amazon",
  "product_id": "B0EXAMPLE",
  "title": "Exact merchant title",
  "merchant_page_url": "https://www.amazon.in/dp/B0EXAMPLE",
  "earnkaro_source_url": "https://earnkaro.com/example/PPS1-Amazon",
  "affiliate_url": "https://bitli.in/example",
  "price": 699,
  "mrp": 3499,
  "category": "Health",
  "rating_count": 500,
  "commission_pct": 5.5,
  "verification_note": "Price and stock verified on merchant page",
  "terms_snapshot": "Tracking and confirmation terms copied from the current EarnKaro deal page"
}
```

Then run `/preview amazon:B0EXAMPLE` and `/approve amazon:B0EXAMPLE`. The worker publishes only while the deal and its evidence remain fresh.

## Rollback

Redeploy the last known-good Git commit. If the database is involved, pause through `/pause`, preserve the volume, inspect incidents and restore a verified SQLite backup before resuming.
