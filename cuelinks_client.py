#!/usr/bin/env python3
"""Cuelinks V3 integration for Worth It India.

Secrets come only from environment variables. Network calls are explicit CLI
commands, which keeps tests deterministic and prevents accidental publication.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


BASE_URL = "https://developers.cuelinks.com/pub_api/v3"
UTC = timezone.utc


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"Cuelinks API {status}: {message}")
        self.status = status


class CuelinksClient:
    def __init__(self, api_key: str, base_url: str = BASE_URL, attempts: int = 5):
        if not api_key or any(ch in api_key for ch in "\r\n"):
            raise ValueError("A valid CUELINKS_API_KEY is required")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.attempts = attempts

    def request(self, method: str, path: str, *, query: dict | None = None, body: dict | None = None) -> dict:
        url = f"{self.base_url}/{path.lstrip('/')}"
        if query:
            url += "?" + urllib.parse.urlencode(query, doseq=True)
        payload = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=payload, method=method.upper())
        request.add_header("Authorization", f"Token {self.api_key}")
        request.add_header("Accept", "application/json")
        if payload is not None:
            request.add_header("Content-Type", "application/json")

        for attempt in range(self.attempts):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                message = exc.read().decode("utf-8", errors="replace")[:1000]
                if exc.code not in {429, 500, 502, 503, 504} or attempt + 1 >= self.attempts:
                    raise ApiError(exc.code, message) from exc
                retry_after = exc.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 2**attempt + random.random()
            except urllib.error.URLError as exc:
                if attempt + 1 >= self.attempts:
                    raise ApiError(0, str(exc.reason)) from exc
                delay = 2**attempt + random.random()
            time.sleep(min(delay, 20))
        raise AssertionError("retry loop exited unexpectedly")

    def paginated(self, path: str, query: dict | None = None) -> Iterable[dict]:
        page = 1
        params = dict(query or {})
        params.setdefault("per_page", 500)
        while True:
            params["page"] = page
            payload = self.request("GET", path, query=params)
            yield from payload.get("data", [])
            next_page = payload.get("meta", {}).get("next_page")
            if not next_page:
                break
            page = int(next_page)

    def ping(self) -> dict:
        return self.request("GET", "ping")

    def campaigns(self) -> Iterable[dict]:
        return self.paginated("campaigns", {"country_id": 252, "sort": "epc_7d", "order": "desc"})

    def campaign(self, campaign_id: int) -> dict:
        return self.request("GET", f"campaigns/{campaign_id}").get("data", {})

    def offers(self, channel_id: int | None = None) -> Iterable[dict]:
        query: dict[str, Any] = {"sort": "updated_at", "order": "desc"}
        if channel_id is not None:
            query["channel_id"] = channel_id
        return self.paginated("offers", query)

    def convert(self, url: str, channel_id: int, subid: str, referral_url: str, shorten: bool = True) -> dict:
        payload = self.request("POST", "links/convert", body={
            "url": url,
            "channel_id": channel_id,
            "shorten": shorten,
            "subid": subid,
            "referral_url": referral_url,
        }).get("data", {})
        if payload.get("affiliated") is not True:
            raise ApiError(422, "link conversion returned affiliated=false")
        return payload

    def transactions(self, updated_since: str) -> Iterable[dict]:
        return self.paginated("transactions", {"updated_since": updated_since, "sort": "updated_at", "order": "asc"})


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str


def policy_decision(campaign: dict, destination: str, override: str | None = None) -> PolicyDecision:
    if override == "deny":
        return PolicyDecision(False, "merchant override denies this destination")
    if campaign.get("access_status") not in {"open", "approved"}:
        return PolicyDecision(False, f"campaign access is {campaign.get('access_status', 'unknown')}")
    if destination == "telegram" and campaign.get("social_media_publishers_allowed") is not True:
        return PolicyDecision(False, "campaign does not explicitly allow social-media publishers")
    if destination == "website" and campaign.get("deeplink_allowed") is not True:
        return PolicyDecision(False, "campaign does not explicitly allow deep links")
    media = campaign.get("media") or {}
    disallowed = {str(item).lower() for item in media.get("disallowed", [])}
    if destination == "telegram" and any("social" in item for item in disallowed):
        return PolicyDecision(False, "campaign media rules disallow social promotion")
    if override == "allow":
        return PolicyDecision(True, "explicit reviewed override allows destination")
    return PolicyDecision(False, "campaign requires one-time merchant policy review")


class AffiliateStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS campaigns (
          campaign_id INTEGER PRIMARY KEY, name TEXT NOT NULL, domain TEXT,
          access_status TEXT NOT NULL, social_allowed INTEGER NOT NULL,
          deeplink_allowed INTEGER NOT NULL, epc_7d REAL, policy_json TEXT NOT NULL,
          fetched_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS policy_overrides (
          campaign_id INTEGER NOT NULL, destination TEXT NOT NULL,
          decision TEXT NOT NULL CHECK(decision IN ('allow','deny')),
          note TEXT NOT NULL, reviewed_at TEXT NOT NULL,
          PRIMARY KEY(campaign_id, destination)
        );
        CREATE TABLE IF NOT EXISTS offers (
          offer_id INTEGER PRIMARY KEY, campaign_id INTEGER NOT NULL, title TEXT NOT NULL,
          offer_type TEXT NOT NULL, original_price REAL, discount_price REAL,
          percent_off REAL, tracking_url TEXT, end_date TEXT, source_json TEXT NOT NULL,
          fetched_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS transactions (
          transaction_id INTEGER PRIMARY KEY, campaign_id INTEGER NOT NULL,
          transaction_date TEXT, status TEXT NOT NULL, sale_amount REAL,
          commission REAL, currency TEXT, channel_id INTEGER, sub_id TEXT,
          source_json TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sync_state (
          name TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        """)
        self.db.commit()

    def upsert_campaign(self, value: dict) -> None:
        self.db.execute("""INSERT INTO campaigns VALUES (?,?,?,?,?,?,?,?,?)
          ON CONFLICT(campaign_id) DO UPDATE SET
          name=excluded.name,domain=excluded.domain,access_status=excluded.access_status,
          social_allowed=excluded.social_allowed,deeplink_allowed=excluded.deeplink_allowed,
          epc_7d=excluded.epc_7d,policy_json=excluded.policy_json,fetched_at=excluded.fetched_at""", (
            int(value["id"]), value["name"], value.get("domain"), value.get("access_status", "unknown"),
            int(value.get("social_media_publishers_allowed") is True), int(value.get("deeplink_allowed") is True),
            float(value.get("epc_7d") or 0), json.dumps(value, ensure_ascii=False), utc_now()))

    def upsert_offer(self, value: dict) -> None:
        self.db.execute("""INSERT INTO offers VALUES (?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(offer_id) DO UPDATE SET campaign_id=excluded.campaign_id,title=excluded.title,
          offer_type=excluded.offer_type,original_price=excluded.original_price,
          discount_price=excluded.discount_price,percent_off=excluded.percent_off,
          tracking_url=excluded.tracking_url,end_date=excluded.end_date,
          source_json=excluded.source_json,fetched_at=excluded.fetched_at""", (
            int(value["id"]), int(value["campaign_id"]), value["title"], value.get("offer_type", "deal"),
            value.get("original_price"), value.get("discount_price"), value.get("percent_off"),
            value.get("tracking_url"), value.get("end_date"), json.dumps(value, ensure_ascii=False), utc_now()))

    def upsert_transaction(self, value: dict) -> None:
        self.db.execute("""INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(transaction_id) DO UPDATE SET status=excluded.status,sale_amount=excluded.sale_amount,
          commission=excluded.commission,source_json=excluded.source_json,updated_at=excluded.updated_at""", (
            int(value["id"]), int(value["campaign_id"]), value.get("transaction_date"), value["status"],
            value.get("sale_amount"), value.get("user_commission"), value.get("currency"), value.get("channel_id"),
            value.get("sub_id"), json.dumps(value, ensure_ascii=False), value.get("updated_at", utc_now())))

    def override(self, campaign_id: int, destination: str) -> str | None:
        row = self.db.execute("SELECT decision FROM policy_overrides WHERE campaign_id=? AND destination=?", (campaign_id, destination)).fetchone()
        return row[0] if row else None

    def set_override(self, campaign_id: int, destination: str, decision: str, note: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO policy_overrides VALUES (?,?,?,?,?)", (campaign_id, destination, decision, note, utc_now()))
        self.db.commit()

    def state(self, name: str, default: str) -> str:
        row = self.db.execute("SELECT value FROM sync_state WHERE name=?", (name,)).fetchone()
        return row[0] if row else default

    def set_state(self, name: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO sync_state VALUES (?,?,?)", (name, value, utc_now()))
        self.db.commit()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Worth It India Cuelinks V3 connector")
    parser.add_argument("--database", default="data/affiliate.db")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ping")
    sub.add_parser("sync-campaigns")
    offers = sub.add_parser("sync-offers")
    offers.add_argument("--channel-id", type=int)
    tx = sub.add_parser("sync-transactions")
    tx.add_argument("--since")
    check = sub.add_parser("policy-check")
    check.add_argument("campaign_id", type=int)
    check.add_argument("destination", choices=["telegram", "website"])
    review = sub.add_parser("policy-review")
    review.add_argument("campaign_id", type=int)
    review.add_argument("destination", choices=["telegram", "website"])
    review.add_argument("decision", choices=["allow", "deny"])
    review.add_argument("note")
    convert = sub.add_parser("convert")
    convert.add_argument("url")
    convert.add_argument("--campaign-id", type=int, required=True)
    convert.add_argument("--channel-id", type=int, required=True)
    convert.add_argument("--destination", choices=["telegram", "website"], required=True)
    convert.add_argument("--subid", required=True)
    convert.add_argument("--referral-url", required=True)
    sub.add_parser("dashboard")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = AffiliateStore(Path(args.database))
    if args.command == "policy-review":
        store.set_override(args.campaign_id, args.destination, args.decision, args.note)
        print("policy review saved")
        return 0
    if args.command == "dashboard":
        totals = store.db.execute("SELECT COUNT(*), COALESCE(SUM(commission),0) FROM transactions WHERE status IN ('validated','payable','invoice_raised','paid')").fetchone()
        print(json.dumps({"campaigns": store.db.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0], "offers": store.db.execute("SELECT COUNT(*) FROM offers").fetchone()[0], "confirmed_transactions": totals[0], "confirmed_commission": round(totals[1], 2)}, indent=2))
        return 0

    key = os.environ.get("CUELINKS_API_KEY", "")
    client = CuelinksClient(key)
    if args.command == "ping":
        print(json.dumps(client.ping(), indent=2))
    elif args.command == "sync-campaigns":
        count = 0
        for campaign in client.campaigns():
            details = client.campaign(int(campaign["id"]))
            store.upsert_campaign(details)
            count += 1
        store.db.commit()
        print(f"synced {count} India campaigns")
    elif args.command == "sync-offers":
        count = 0
        for offer in client.offers(args.channel_id):
            store.upsert_offer(offer)
            count += 1
        store.db.commit()
        print(f"synced {count} live offers")
    elif args.command == "sync-transactions":
        default_since = (datetime.now(UTC) - timedelta(days=30)).replace(microsecond=0).isoformat()
        since = args.since or store.state("transactions_updated_since", default_since)
        latest = since
        count = 0
        for transaction in client.transactions(since):
            store.upsert_transaction(transaction)
            latest = max(latest, transaction.get("updated_at", latest))
            count += 1
        store.db.commit()
        store.set_state("transactions_updated_since", latest)
        print(f"synced {count} transactions through {latest}")
    elif args.command in {"policy-check", "convert"}:
        row = store.db.execute("SELECT policy_json FROM campaigns WHERE campaign_id=?", (args.campaign_id,)).fetchone()
        if not row:
            raise RuntimeError("campaign is not synced")
        campaign = json.loads(row[0])
        decision = policy_decision(campaign, args.destination, store.override(args.campaign_id, args.destination))
        if args.command == "policy-check":
            print(json.dumps(decision.__dict__, indent=2))
            return 0 if decision.allowed else 2
        if not decision.allowed:
            raise RuntimeError(f"conversion blocked: {decision.reason}")
        result = client.convert(args.url, args.channel_id, args.subid, args.referral_url)
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ApiError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
