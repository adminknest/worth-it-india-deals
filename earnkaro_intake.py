#!/usr/bin/env python3
"""Browser-assisted EarnKaro adapter for Worth It India.

EarnKaro does not expose a documented public publisher API for this account. This
adapter keeps the only manual step explicit: verify a merchant page in the browser,
create/copy the Profit Link in EarnKaro, then record the evidence here. Nothing is
approved or published automatically.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path

from dealbot import Candidate, now_utc, score_candidate, store_from_config


ALLOWED_EARNKARO_SOURCE_HOSTS = {"earnkaro.com"}
REPORT_STATUSES = {"pending", "confirmed", "cancelled", "rejected", "paid"}


def host_of(url: str) -> str:
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"Invalid URL: {url!r}")
    return parsed.netloc.lower().removeprefix("www.")


def ensure_schema(store) -> None:
    store.db.executescript(
        """
        CREATE TABLE IF NOT EXISTS earnkaro_evidence (
            product_key TEXT PRIMARY KEY,
            earnkaro_source_url TEXT NOT NULL,
            merchant_page_url TEXT NOT NULL,
            affiliate_url TEXT NOT NULL,
            price_verified_at TEXT NOT NULL,
            stock_verified INTEGER NOT NULL,
            verification_note TEXT NOT NULL,
            terms_snapshot TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            FOREIGN KEY(product_key) REFERENCES deals(product_key)
        );
        CREATE TABLE IF NOT EXISTS commission_events (
            source_event_id TEXT PRIMARY KEY,
            occurred_at TEXT NOT NULL,
            product_key TEXT,
            merchant TEXT NOT NULL,
            order_amount REAL NOT NULL DEFAULT 0,
            commission_amount REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            imported_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_commission_status
            ON commission_events(status, occurred_at DESC);
        """
    )
    store.db.commit()


def record_verified_deal(args, config, store) -> str:
    if not args.stock_verified:
        raise ValueError("A deal cannot enter the queue until stock is verified")
    if host_of(args.earnkaro_source_url) not in ALLOWED_EARNKARO_SOURCE_HOSTS:
        raise ValueError("The deal source must be an earnkaro.com page")

    observed_at = now_utc()
    candidate = Candidate(
        source="earnkaro-browser-verified",
        merchant=args.merchant,
        product_id=args.product_id,
        title=args.title,
        url=args.merchant_page_url,
        price=args.price,
        mrp=args.mrp,
        category=args.category,
        in_stock=True,
        observed_at=observed_at,
        rating_count=args.rating_count,
        commission_pct=args.commission_pct,
    )
    score, rejection = score_candidate(candidate, config)
    store.upsert(candidate, score, rejection, int(config.get("repost_cooldown_hours", 72)))
    store.attach_link(candidate.key, args.affiliate_url, config["allowed_affiliate_hosts"])
    store.record_evidence(
        candidate.key,
        args.merchant_page_url,
        "merchant-page-browser-check",
        observed_at,
        args.price,
        True,
        args.verification_note,
        actor="earnkaro-intake",
    )
    now = now_utc().isoformat()
    store.db.execute(
        """INSERT OR REPLACE INTO earnkaro_evidence
           (product_key,earnkaro_source_url,merchant_page_url,affiliate_url,
            price_verified_at,stock_verified,verification_note,terms_snapshot,recorded_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            candidate.key,
            args.earnkaro_source_url,
            args.merchant_page_url,
            args.affiliate_url,
            observed_at.isoformat(),
            1,
            args.verification_note,
            args.terms_snapshot,
            now,
        ),
    )
    store.db.commit()
    return candidate.key


def stable_event_id(row: dict) -> str:
    explicit = (row.get("event_id") or row.get("order_id") or "").strip()
    if explicit:
        return f"earnkaro:{explicit}"
    material = "|".join(
        str(row.get(name, "")).strip()
        for name in ("occurred_at", "merchant", "product_key", "order_amount", "commission_amount", "status")
    )
    return "earnkaro:sha256:" + hashlib.sha256(material.encode()).hexdigest()


def import_report(path: Path, store) -> tuple[int, int]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    inserted = duplicates = 0
    for row in rows:
        status = str(row.get("status", "pending")).strip().lower()
        if status not in REPORT_STATUSES:
            raise ValueError(f"Unsupported commission status: {status!r}")
        occurred = str(row.get("occurred_at", "")).strip()
        datetime.fromisoformat(occurred.replace("Z", "+00:00"))
        event_id = stable_event_id(row)
        before = store.db.total_changes
        store.db.execute(
            """INSERT OR IGNORE INTO commission_events
               (source_event_id,occurred_at,product_key,merchant,order_amount,
                commission_amount,status,raw_json,imported_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                event_id,
                occurred,
                str(row.get("product_key", "")).strip() or None,
                str(row.get("merchant", "")).strip(),
                float(row.get("order_amount", 0) or 0),
                float(row.get("commission_amount", 0) or 0),
                status,
                json.dumps(row, ensure_ascii=False, sort_keys=True),
                now_utc().isoformat(),
            ),
        )
        if store.db.total_changes > before:
            inserted += 1
        else:
            duplicates += 1
    store.db.commit()
    return inserted, duplicates


def dashboard(store) -> dict:
    queued = store.db.execute(
        "SELECT COUNT(*) FROM deals WHERE status='active' AND affiliate_url IS NOT NULL"
    ).fetchone()[0]
    approved = store.db.execute(
        "SELECT COUNT(*) FROM deals WHERE status='active' AND approved=1 AND affiliate_url IS NOT NULL"
    ).fetchone()[0]
    posted = store.db.execute("SELECT COUNT(*) FROM deals WHERE status='posted'").fetchone()[0]
    commissions = {
        row["status"]: {"orders": row["orders"], "commission_inr": round(row["amount"], 2)}
        for row in store.db.execute(
            """SELECT status, COUNT(*) AS orders, COALESCE(SUM(commission_amount),0) AS amount
               FROM commission_events GROUP BY status"""
        ).fetchall()
    }
    return {"queued_with_links": queued, "approved": approved, "posted": posted, "commissions": commissions}


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="EarnKaro browser-assisted intake")
    parser.add_argument("--config", default="config.json")
    sub = parser.add_subparsers(dest="command", required=True)

    record = sub.add_parser("record", help="record one merchant-verified EarnKaro deal")
    record.add_argument("--merchant", required=True)
    record.add_argument("--product-id", required=True)
    record.add_argument("--title", required=True)
    record.add_argument("--merchant-page-url", required=True)
    record.add_argument("--earnkaro-source-url", required=True)
    record.add_argument("--affiliate-url", required=True)
    record.add_argument("--price", type=float, required=True)
    record.add_argument("--mrp", type=float, required=True)
    record.add_argument("--category", default="General")
    record.add_argument("--rating-count", type=int, default=0)
    record.add_argument("--commission-pct", type=float, default=0)
    record.add_argument("--stock-verified", action="store_true")
    record.add_argument("--verification-note", required=True)
    record.add_argument("--terms-snapshot", required=True)

    report = sub.add_parser("import-report", help="import an exported/manual commission CSV")
    report.add_argument("path", type=Path)
    sub.add_parser("dashboard")
    sub.add_parser("readiness")

    args = parser.parse_args(argv)
    config_path = Path(args.config).resolve()
    config, store = store_from_config(config_path)
    ensure_schema(store)

    if args.command == "record":
        key = record_verified_deal(args, config, store)
        row = store.get(key)
        print(f"Recorded {key}; status={row['status']}; approval=pending")
    elif args.command == "import-report":
        inserted, duplicates = import_report(args.path, store)
        print(f"Imported {inserted}; duplicates skipped {duplicates}")
    elif args.command == "dashboard":
        print(json.dumps(dashboard(store), ensure_ascii=False, indent=2))
    elif args.command == "readiness":
        print(json.dumps({
            "earnkaro_login": "ready",
            "amazon_traffic_source": "blocked: Telegram source must be registered",
            "telegram_channel": "blocked: public username not yet saved",
            "telegram_bot": "blocked: bot not yet created/admin",
            "publisher": "ready for dry-run",
        }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
