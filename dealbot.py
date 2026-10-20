#!/usr/bin/env python3
"""Lean, approval-first Telegram deals bot.

The bot deliberately does not scrape stores or reverse-engineer EarnKaro. It accepts
authorized CSV/JSON feeds, ranks and deduplicates deals, and publishes only records
that have a verified EarnKaro link plus explicit admin approval.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import os
import sqlite3
import sys
import time
import uuid
import urllib.parse
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable


UTC = timezone.utc
MAX_FEED_BYTES = 5 * 1024 * 1024


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def parse_time(value: str | None) -> datetime:
    if not value:
        return now_utc()
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def canonical_url(raw: str) -> str:
    parsed = urllib.parse.urlsplit(raw.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"Invalid product URL: {raw!r}")
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit((parsed.scheme.lower(), host, path, "", ""))


def as_bool(value: object) -> bool:
    return str(value).strip().lower() not in {"0", "false", "no", "out", "sold_out", ""}


def material_fingerprint(row: object) -> str:
    """Fingerprint every field whose change invalidates a human approval."""
    if isinstance(row, sqlite3.Row):
        values = dict(row)
    elif isinstance(row, dict):
        values = row
    else:
        values = vars(row)
    material = {
        "source": str(values.get("source", "")),
        "merchant": str(values.get("merchant", "")).lower(),
        "product_id": str(values.get("product_id", "")).lower(),
        "title": str(values.get("title", "")),
        "product_url": str(values.get("product_url", values.get("url", ""))),
        "price": round(float(values.get("price", 0)), 2),
        "mrp": round(float(values.get("mrp", 0)), 2),
        "affiliate_url": str(values.get("affiliate_url", "")),
    }
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True)
class Candidate:
    source: str
    merchant: str
    product_id: str
    title: str
    url: str
    price: float
    mrp: float
    category: str
    in_stock: bool
    observed_at: datetime
    rating_count: int = 0
    commission_pct: float = 0.0

    @property
    def key(self) -> str:
        if self.product_id:
            return f"{self.merchant.lower()}:{self.product_id.lower()}"
        digest = hashlib.sha256(canonical_url(self.url).encode()).hexdigest()[:20]
        return f"{self.merchant.lower()}:{digest}"

    @property
    def discount_pct(self) -> float:
        if self.mrp <= 0 or self.price <= 0 or self.price > self.mrp:
            return 0.0
        return round((self.mrp - self.price) / self.mrp * 100, 2)


def score_candidate(candidate: Candidate, config: dict) -> tuple[float, str | None]:
    if not candidate.in_stock:
        return 0.0, "sold out"
    if candidate.price <= 0 or candidate.mrp <= 0 or candidate.price > candidate.mrp:
        return 0.0, "invalid price"
    age = now_utc() - candidate.observed_at
    if age > timedelta(minutes=int(config["freshness_minutes"])):
        return 0.0, "stale price"
    discount = candidate.discount_pct
    if discount < float(config["min_discount_pct"]):
        return 0.0, "discount below threshold"
    if candidate.rating_count < int(config["min_rating_count"]):
        return 0.0, "insufficient rating evidence"

    price_value = 12 if candidate.price <= 500 else 8 if candidate.price <= 1500 else 3
    social_proof = min(math.log10(candidate.rating_count + 1) * 5, 20)
    commission = min(candidate.commission_pct * 0.5, 10)
    score = discount * 0.68 + price_value + social_proof + commission
    return round(score, 3), None


def load_config(path: Path) -> dict:
    config = json.loads(path.read_text(encoding="utf-8"))
    defaults = {
        "database": "data/deals.db",
        "scan_interval_seconds": 60,
        "post_interval_seconds": 1800,
        "max_posts_per_day": 24,
        "repost_cooldown_hours": 72,
        "freshness_minutes": 30,
        "min_discount_pct": 35,
        "min_rating_count": 50,
        "allowed_feed_hosts": [],
        "allowed_affiliate_hosts": ["myntr.it", "bitli.in", "earnkaro.com"],
    }
    merged = defaults | config
    if os.environ.get("DEALBOT_DATABASE"):
        merged["database"] = os.environ["DEALBOT_DATABASE"]
    return merged


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS deals (
                product_key TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                merchant TEXT NOT NULL,
                product_id TEXT,
                title TEXT NOT NULL,
                product_url TEXT NOT NULL,
                price REAL NOT NULL,
                mrp REAL NOT NULL,
                discount_pct REAL NOT NULL,
                category TEXT NOT NULL,
                rating_count INTEGER NOT NULL,
                commission_pct REAL NOT NULL,
                observed_at TEXT NOT NULL,
                score REAL NOT NULL,
                rejection_reason TEXT,
                affiliate_url TEXT,
                approved INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                posted_at TEXT,
                telegram_message_id TEXT,
                deal_fingerprint TEXT,
                manual_rejection_fingerprint TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_deals_ready
                ON deals(status, approved, score DESC, observed_at DESC);
            CREATE TABLE IF NOT EXISTS publication_attempts (
                idempotency_key TEXT PRIMARY KEY,
                product_key TEXT NOT NULL,
                destination TEXT NOT NULL,
                state TEXT NOT NULL,
                error TEXT,
                telegram_message_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS operator_state (
                name TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS verification_evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_key TEXT NOT NULL,
                source_url TEXT NOT NULL,
                evidence_type TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                price REAL NOT NULL,
                in_stock INTEGER NOT NULL,
                note TEXT NOT NULL,
                evidence_hash TEXT NOT NULL UNIQUE,
                recorded_at TEXT NOT NULL,
                FOREIGN KEY(product_key) REFERENCES deals(product_key)
            );
            CREATE INDEX IF NOT EXISTS idx_evidence_product
                ON verification_evidence(product_key, observed_at DESC);
            CREATE TABLE IF NOT EXISTS approvals (
                product_key TEXT PRIMARY KEY,
                deal_fingerprint TEXT NOT NULL,
                actor TEXT NOT NULL,
                approved_at TEXT NOT NULL,
                FOREIGN KEY(product_key) REFERENCES deals(product_key)
            );
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                occurred_at TEXT NOT NULL,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                product_key TEXT,
                details_json TEXT NOT NULL,
                previous_hash TEXT NOT NULL,
                event_hash TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS worker_leases (
                name TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                severity TEXT NOT NULL,
                component TEXT NOT NULL,
                error_class TEXT NOT NULL,
                message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL,
                resolved_at TEXT
            );
            """
        )
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(deals)")}
        if "deal_fingerprint" not in columns:
            self.db.execute("ALTER TABLE deals ADD COLUMN deal_fingerprint TEXT")
        if "manual_rejection_fingerprint" not in columns:
            self.db.execute("ALTER TABLE deals ADD COLUMN manual_rejection_fingerprint TEXT")
        for row in self.db.execute("SELECT * FROM deals WHERE deal_fingerprint IS NULL OR deal_fingerprint='' ").fetchall():
            self.db.execute(
                "UPDATE deals SET deal_fingerprint=? WHERE product_key=?",
                (material_fingerprint(row), row["product_key"]),
            )
        legacy_approvals = self.db.execute(
            """SELECT COUNT(*) FROM deals d LEFT JOIN approvals a ON a.product_key=d.product_key
               WHERE d.approved=1 AND a.product_key IS NULL"""
        ).fetchone()[0]
        if legacy_approvals:
            self.db.execute(
                """UPDATE deals SET approved=0 WHERE approved=1 AND product_key NOT IN
                   (SELECT product_key FROM approvals)"""
            )
            self.audit("migration", "legacy_approvals.reset", None, {"count": legacy_approvals})
        self.db.commit()

    def audit(self, actor: str, action: str, product_key: str | None = None, details: dict | None = None) -> str:
        previous = self.db.execute("SELECT event_hash FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
        previous_hash = previous[0] if previous else "GENESIS"
        occurred_at = now_utc().isoformat()
        details_json = json.dumps(details or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        material = "|".join((previous_hash, occurred_at, actor, action, product_key or "", details_json))
        event_hash = hashlib.sha256(material.encode()).hexdigest()
        self.db.execute(
            """INSERT INTO audit_events
               (occurred_at,actor,action,product_key,details_json,previous_hash,event_hash)
               VALUES (?,?,?,?,?,?,?)""",
            (occurred_at, actor, action, product_key, details_json, previous_hash, event_hash),
        )
        return event_hash

    def verify_audit_chain(self) -> bool:
        previous_hash = "GENESIS"
        for row in self.db.execute("SELECT * FROM audit_events ORDER BY id").fetchall():
            material = "|".join((previous_hash, row["occurred_at"], row["actor"], row["action"], row["product_key"] or "", row["details_json"]))
            expected = hashlib.sha256(material.encode()).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != expected:
                return False
            previous_hash = row["event_hash"]
        return True

    def upsert(self, candidate: Candidate, score: float, rejection: str | None,
               repost_cooldown_hours: int = 72) -> None:
        old = self.db.execute(
            "SELECT * FROM deals WHERE product_key=?",
            (candidate.key,),
        ).fetchone()
        preserved = dict(old) if old else {}
        prospective = {
            "source": candidate.source, "merchant": candidate.merchant,
            "product_id": candidate.product_id, "title": candidate.title,
            "product_url": canonical_url(candidate.url), "price": candidate.price,
            "mrp": candidate.mrp, "affiliate_url": preserved.get("affiliate_url") or "",
        }
        fingerprint = material_fingerprint(prospective)
        material_changed = bool(old and old["deal_fingerprint"] != fingerprint)
        manual_rejection_fingerprint = preserved.get("manual_rejection_fingerprint")
        if material_changed:
            manual_rejection_fingerprint = None
        status = preserved.get("status", "active")
        rejection_reason = rejection
        repost_reactivated = False
        if manual_rejection_fingerprint == fingerprint:
            status = "rejected"
            rejection_reason = preserved.get("rejection_reason") or "rejected by administrator"
        elif status == "posted":
            posted_at = parse_time(preserved.get("posted_at")) if preserved.get("posted_at") else now_utc()
            if now_utc() - posted_at >= timedelta(hours=repost_cooldown_hours):
                status = "active"
                repost_reactivated = True
        elif rejection and status != "posted":
            status = "rejected"
        elif not rejection and status == "rejected":
            status = "active"
        approved = 0 if material_changed or repost_reactivated else preserved.get("approved", 0)
        if material_changed or repost_reactivated:
            self.db.execute("DELETE FROM approvals WHERE product_key=?", (candidate.key,))
        self.db.execute(
            """
            INSERT OR REPLACE INTO deals (
                product_key, source, merchant, product_id, title, product_url, price, mrp,
                discount_pct, category, rating_count, commission_pct, observed_at, score,
                rejection_reason, affiliate_url, approved, status, posted_at,
                telegram_message_id, deal_fingerprint, manual_rejection_fingerprint, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                candidate.key, candidate.source, candidate.merchant, candidate.product_id,
                candidate.title, canonical_url(candidate.url), candidate.price, candidate.mrp,
                candidate.discount_pct, candidate.category, candidate.rating_count,
                candidate.commission_pct, candidate.observed_at.isoformat(), score, rejection_reason,
                preserved.get("affiliate_url"), approved, status,
                preserved.get("posted_at"), preserved.get("telegram_message_id"), fingerprint,
                manual_rejection_fingerprint, now_utc().isoformat(),
            ),
        )
        if not old:
            self.audit("pipeline", "candidate.created", candidate.key, {"score": score, "rejection": rejection})
        elif material_changed:
            self.audit("pipeline", "candidate.material_changed", candidate.key, {"approval_reset": True})
        elif repost_reactivated:
            self.audit("pipeline", "candidate.repost_eligible", candidate.key, {"approval_reset": True, "cooldown_hours": repost_cooldown_hours})
        self.db.commit()

    def shortlist(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM deals WHERE status='active' ORDER BY score DESC, observed_at DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def get(self, key: str) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM deals WHERE product_key=?", (key,)).fetchone()
        if not row:
            raise KeyError(key)
        return row

    def attach_link(self, key: str, link: str, allowed_hosts: list[str]) -> None:
        host = urllib.parse.urlsplit(link).netloc.lower().removeprefix("www.")
        if host not in {h.lower() for h in allowed_hosts}:
            raise ValueError(f"Affiliate host {host!r} is not allowlisted")
        row = self.get(key)
        changed = (row["affiliate_url"] or "") != link
        values = dict(row)
        values["affiliate_url"] = link
        fingerprint = material_fingerprint(values)
        self.db.execute(
            "UPDATE deals SET affiliate_url=?, approved=?, deal_fingerprint=?, updated_at=? WHERE product_key=?",
            (link, 0 if changed else row["approved"], fingerprint, now_utc().isoformat(), key),
        )
        if changed:
            self.db.execute("DELETE FROM approvals WHERE product_key=?", (key,))
            self.audit("pipeline", "affiliate_link.changed", key, {"host": host, "approval_reset": True})
        self.db.commit()

    def record_evidence(self, key: str, source_url: str, evidence_type: str, observed_at: datetime,
                        price: float, in_stock: bool, note: str, actor: str = "pipeline") -> str:
        self.get(key)
        canonical_source = canonical_url(source_url)
        payload = {
            "product_key": key, "source_url": canonical_source, "evidence_type": evidence_type,
            "observed_at": observed_at.isoformat(), "price": round(price, 2),
            "in_stock": bool(in_stock), "note": note.strip(),
        }
        evidence_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.db.execute(
            """INSERT OR IGNORE INTO verification_evidence
               (product_key,source_url,evidence_type,observed_at,price,in_stock,note,evidence_hash,recorded_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (key, canonical_source, evidence_type, observed_at.isoformat(), price, int(in_stock), note.strip(), evidence_hash, now_utc().isoformat()),
        )
        self.audit(actor, "verification.recorded", key, {"evidence_hash": evidence_hash, "price": price, "in_stock": bool(in_stock)})
        self.db.commit()
        return evidence_hash

    def latest_evidence(self, key: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM verification_evidence WHERE product_key=? ORDER BY observed_at DESC,id DESC LIMIT 1",
            (key,),
        ).fetchone()

    def approve(self, key: str, actor: str = "administrator") -> None:
        row = self.get(key)
        if row["status"] != "active":
            raise ValueError("Only an active deal can be approved")
        if not row["affiliate_url"]:
            raise ValueError("Attach a permitted affiliate link before approval")
        evidence = self.latest_evidence(key)
        if not evidence or not evidence["in_stock"] or abs(float(evidence["price"]) - float(row["price"])) > 0.009:
            raise ValueError("Fresh matching price and stock evidence is required before approval")
        fingerprint = material_fingerprint(row)
        now = datetime.now(UTC).isoformat(timespec="microseconds")
        self.db.execute("UPDATE deals SET approved=1, deal_fingerprint=?, updated_at=? WHERE product_key=?", (fingerprint, now, key))
        self.db.execute(
            "INSERT OR REPLACE INTO approvals(product_key,deal_fingerprint,actor,approved_at) VALUES (?,?,?,?)",
            (key, fingerprint, actor, now),
        )
        self.audit(actor, "deal.approved", key, {"deal_fingerprint": fingerprint})
        self.db.commit()

    def reject(self, key: str, reason: str = "rejected by administrator") -> None:
        self.get(key)
        self.db.execute(
            """UPDATE deals SET approved=0, status='rejected', rejection_reason=?,
               manual_rejection_fingerprint=deal_fingerprint, updated_at=? WHERE product_key=?""",
            (reason, now_utc().isoformat(), key),
        )
        self.db.execute("DELETE FROM approvals WHERE product_key=?", (key,))
        self.audit("administrator", "deal.rejected", key, {"reason": reason})
        self.db.commit()

    def state(self, name: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM operator_state WHERE name=?", (name,)).fetchone()
        return row[0] if row else default

    def set_state(self, name: str, value: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO operator_state(name,value,updated_at) VALUES (?,?,?)",
            (name, value, now_utc().isoformat()),
        )
        self.audit("operator", "operator_state.changed", None, {"name": name, "value": value})
        self.db.commit()

    def acquire_lease(self, name: str, owner: str, ttl_seconds: int = 90) -> bool:
        now = now_utc()
        expires = (now + timedelta(seconds=ttl_seconds)).isoformat()
        with self.db:
            current = self.db.execute("SELECT owner,expires_at FROM worker_leases WHERE name=?", (name,)).fetchone()
            if current and current["owner"] != owner and parse_time(current["expires_at"]) > now:
                return False
            self.db.execute(
                "INSERT OR REPLACE INTO worker_leases(name,owner,expires_at,updated_at) VALUES (?,?,?,?)",
                (name, owner, expires, now.isoformat()),
            )
        return True

    def incident(self, severity: str, component: str, exc: Exception) -> None:
        self.db.execute(
            "INSERT INTO incidents(severity,component,error_class,message,created_at) VALUES (?,?,?,?,?)",
            (severity, component, type(exc).__name__, str(exc)[:500], now_utc().isoformat()),
        )
        self.audit("pipeline", "incident.opened", None, {"severity": severity, "component": component, "error_class": type(exc).__name__})
        self.db.commit()

    def doctor(self, config: dict) -> dict:
        integrity = self.db.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = len(self.db.execute("PRAGMA foreign_key_check").fetchall())
        uncertain = self.db.execute("SELECT COUNT(*) FROM publication_attempts WHERE state='uncertain'").fetchone()[0]
        stale_sending_cutoff = (now_utc() - timedelta(minutes=10)).isoformat()
        stale_sending = self.db.execute(
            "SELECT COUNT(*) FROM publication_attempts WHERE state='sending' AND updated_at<?",
            (stale_sending_cutoff,),
        ).fetchone()[0]
        invalid_approvals = self.db.execute(
            """SELECT COUNT(*) FROM approvals a JOIN deals d ON d.product_key=a.product_key
               WHERE a.deal_fingerprint<>d.deal_fingerprint OR d.approved<>1"""
        ).fetchone()[0]
        orphan_approval_flags = self.db.execute(
            """SELECT COUNT(*) FROM deals d LEFT JOIN approvals a ON a.product_key=d.product_key
               WHERE d.approved=1 AND a.product_key IS NULL"""
        ).fetchone()[0]
        checks = {
            "database_integrity": integrity,
            "foreign_key_errors": foreign_keys,
            "audit_chain_valid": self.verify_audit_chain(),
            "uncertain_publications": uncertain,
            "stale_sending_attempts": stale_sending,
            "invalid_approvals": invalid_approvals,
            "orphan_approval_flags": orphan_approval_flags,
            "paused": self.state("paused", "0") == "1",
        }
        checks["healthy"] = (
            integrity == "ok" and foreign_keys == 0 and checks["audit_chain_valid"]
            and uncertain == 0 and stale_sending == 0 and invalid_approvals == 0
            and orphan_approval_flags == 0
        )
        return checks

    def reserve_publication(self, row: sqlite3.Row, destination: str) -> str | None:
        approval = self.db.execute(
            "SELECT approved_at FROM approvals WHERE product_key=?", (row["product_key"],)
        ).fetchone()
        approval_cycle = approval[0] if approval else "unapproved"
        material = f"{destination}|{row['product_key']}|{row['price']:.2f}|{row['affiliate_url']}|{approval_cycle}"
        key = hashlib.sha256(material.encode()).hexdigest()
        existing = self.db.execute(
            "SELECT state FROM publication_attempts WHERE idempotency_key=?", (key,)
        ).fetchone()
        if existing and existing[0] in {"sending", "uncertain", "sent"}:
            return None
        now = now_utc().isoformat()
        self.db.execute(
            """INSERT OR REPLACE INTO publication_attempts
               (idempotency_key,product_key,destination,state,error,telegram_message_id,created_at,updated_at)
               VALUES (?,?,?,'sending',NULL,NULL,COALESCE((SELECT created_at FROM publication_attempts WHERE idempotency_key=?),?),?)""",
            (key, row["product_key"], destination, key, now, now),
        )
        self.db.commit()
        return key

    def finish_publication(self, attempt_key: str, state: str, message_id: str | None = None, error: str | None = None) -> None:
        self.db.execute(
            "UPDATE publication_attempts SET state=?, error=?, telegram_message_id=?, updated_at=? WHERE idempotency_key=?",
            (state, error, message_id, now_utc().isoformat(), attempt_key),
        )
        self.db.commit()

    def reconcile_publication(self, attempt_key: str, outcome: str, message_id: str | None, actor: str) -> None:
        attempt = self.db.execute(
            "SELECT * FROM publication_attempts WHERE idempotency_key=?", (attempt_key,)
        ).fetchone()
        if not attempt:
            raise KeyError(attempt_key)
        if attempt["state"] not in {"sending", "uncertain"}:
            raise ValueError(f"Attempt is already final: {attempt['state']}")
        if outcome == "sent":
            if not message_id:
                raise ValueError("Telegram message ID is required for a sent outcome")
            self.finish_publication(attempt_key, "sent", message_id=message_id)
            self.mark_posted(attempt["product_key"], message_id)
        elif outcome == "failed":
            self.finish_publication(attempt_key, "failed", error="manually reconciled as not sent")
        else:
            raise ValueError("Outcome must be sent or failed")
        self.audit(actor, "publication.reconciled", attempt["product_key"], {"attempt": attempt_key, "outcome": outcome, "message_id": message_id})
        self.db.commit()

    def next_publishable(self, config: dict) -> sqlite3.Row | None:
        cutoff = now_utc() - timedelta(minutes=int(config["freshness_minutes"]))
        rows = self.db.execute(
            """SELECT * FROM deals
               WHERE status='active' AND approved=1 AND affiliate_url IS NOT NULL
               ORDER BY score DESC, observed_at DESC"""
        ).fetchall()
        for row in rows:
            approval = self.db.execute("SELECT deal_fingerprint FROM approvals WHERE product_key=?", (row["product_key"],)).fetchone()
            evidence = self.latest_evidence(row["product_key"])
            if not approval or approval[0] != row["deal_fingerprint"]:
                continue
            if not evidence or parse_time(evidence["observed_at"]) < cutoff:
                continue
            if not evidence["in_stock"] or abs(float(evidence["price"]) - float(row["price"])) > 0.009:
                continue
            if parse_time(row["observed_at"]) >= cutoff:
                return row
        return None

    def can_post(self, config: dict) -> tuple[bool, str | None]:
        today = now_utc().date().isoformat()
        count = self.db.execute(
            "SELECT COUNT(*) FROM deals WHERE status='posted' AND substr(posted_at,1,10)=?", (today,)
        ).fetchone()[0]
        if count >= int(config["max_posts_per_day"]):
            return False, "daily cap reached"
        last = self.db.execute("SELECT posted_at FROM deals WHERE status='posted' ORDER BY posted_at DESC LIMIT 1").fetchone()
        if last and now_utc() - parse_time(last[0]) < timedelta(seconds=int(config["post_interval_seconds"])):
            return False, "post interval not reached"
        return True, None

    def mark_posted(self, key: str, message_id: str) -> None:
        self.db.execute(
            "UPDATE deals SET status='posted', posted_at=?, telegram_message_id=?, updated_at=? WHERE product_key=?",
            (now_utc().isoformat(), message_id, now_utc().isoformat(), key),
        )
        self.audit("publisher", "telegram.sent", key, {"message_id": message_id})
        self.db.commit()


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts: set[str]):
        super().__init__()
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        host = urllib.parse.urlsplit(newurl).netloc.lower().removeprefix("www.")
        if host not in self.allowed_hosts:
            raise ValueError(f"Redirected feed host {host!r} is not allowlisted")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def candidate_from_mapping(row: dict) -> Candidate:
    return Candidate(
        source=str(row.get("source", "manual")), merchant=str(row["merchant"]),
        product_id=str(row.get("product_id", "")), title=str(row["title"]),
        url=str(row["url"]), price=float(row["price"]), mrp=float(row["mrp"]),
        category=str(row.get("category", "General")), in_stock=as_bool(row.get("in_stock", True)),
        observed_at=parse_time(row.get("observed_at")), rating_count=int(float(row.get("rating_count", 0))),
        commission_pct=float(row.get("commission_pct", 0) or 0),
    )


def load_candidates(path_or_url: str, config: dict) -> Iterable[Candidate]:
    parsed = urllib.parse.urlsplit(path_or_url)
    source_observed_at: str | None = None
    if parsed.scheme in {"http", "https"}:
        host = parsed.netloc.lower().removeprefix("www.")
        if host not in {h.lower() for h in config["allowed_feed_hosts"]}:
            raise ValueError(f"Feed host {host!r} is not allowlisted")
        allowed_hosts = {h.lower() for h in config["allowed_feed_hosts"]}
        opener = urllib.request.build_opener(SafeRedirectHandler(allowed_hosts))
        with opener.open(path_or_url, timeout=20) as response:
            payload_bytes = response.read(MAX_FEED_BYTES + 1)
        if len(payload_bytes) > MAX_FEED_BYTES:
            raise ValueError("Remote feed exceeds the 5 MiB safety limit")
        payload = payload_bytes.decode("utf-8")
        rows = json.loads(payload)
    else:
        path = Path(path_or_url)
        source_observed_at = datetime.fromtimestamp(path.stat().st_mtime, UTC).replace(microsecond=0).isoformat()
        if path.suffix.lower() == ".csv":
            with path.open(newline="", encoding="utf-8-sig") as fh:
                rows = list(csv.DictReader(fh))
        else:
            rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError("Feed payload must be a JSON array")
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Every feed item must be an object")
        normalized = dict(row)
        if not normalized.get("observed_at"):
            if not source_observed_at:
                raise ValueError("Remote feeds must supply observed_at for every product")
            normalized["observed_at"] = source_observed_at
        yield candidate_from_mapping(normalized)


def format_post(row: sqlite3.Row) -> str:
    checked = parse_time(row["observed_at"]).astimezone().strftime("%d %b %Y, %H:%M")
    return (
        f"<b>{html.escape(row['title'])}</b>\n\n"
        f"Price seen: <b>₹{row['price']:,.0f}</b> | MRP: ₹{row['mrp']:,.0f} "
        f"({row['discount_pct']:.0f}% off)\n"
        f"Checked: {checked}. Price, stock and delivery can change.\n\n"
        f"<a href=\"{html.escape(row['affiliate_url'], quote=True)}\">View deal</a>\n"
        f"<i>Affiliate link — we may earn a commission at no extra cost to you.</i>"
    )


def telegram_send(text: str) -> str:
    token = os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHANNEL_ID")
    if not token or not chat_id:
        raise RuntimeError("TG_BOT_TOKEN and TG_CHANNEL_ID are required")
    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": "false",
    }).encode()
    request = urllib.request.Request(endpoint, data=body, method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError(f"Telegram rejected the message: {result.get('description', 'unknown error')}")
    return str(result["result"]["message_id"])


def store_from_config(config_path: Path) -> tuple[dict, Store]:
    config = load_config(config_path)
    db_path = Path(config["database"])
    if not db_path.is_absolute():
        db_path = config_path.parent / db_path
    return config, Store(db_path)


def ingest(source: str, config: dict, store: Store) -> tuple[int, int]:
    accepted = rejected = 0
    for candidate in load_candidates(source, config):
        score, reason = score_candidate(candidate, config)
        store.upsert(candidate, score, reason, int(config.get("repost_cooldown_hours", 72)))
        rejected += bool(reason)
        accepted += not bool(reason)
    return accepted, rejected


def print_rows(rows: Iterable[sqlite3.Row]) -> None:
    for r in rows:
        link = "linked" if r["affiliate_url"] else "needs-link"
        approval = "approved" if r["approved"] else "pending"
        print(f"{r['product_key']} | score {r['score']:.2f} | {r['discount_pct']:.0f}% | ₹{r['price']:.0f} | {link} | {approval} | {r['title']}")


def publish_once(config: dict, store: Store, dry_run: bool) -> bool:
    if store.state("paused", "0") == "1":
        print("No post: operator is paused")
        return False
    allowed, reason = store.can_post(config)
    if not allowed:
        print(f"No post: {reason}")
        return False
    row = store.next_publishable(config)
    if not row:
        print("No post: no fresh approved deal with an affiliate link")
        return False
    text = format_post(row)
    if dry_run:
        print(text)
        return True
    destination = os.environ.get("TG_CHANNEL_ID", "telegram-channel")
    attempt_key = store.reserve_publication(row, destination)
    if not attempt_key:
        print("No post: an identical send is already sent or requires manual reconciliation")
        return False
    try:
        message_id = telegram_send(text)
    except urllib.error.HTTPError as exc:
        if exc.code >= 500:
            store.finish_publication(attempt_key, "uncertain", error=f"HTTP {exc.code}")
            store.set_state("paused", "1")
            store.incident("critical", "telegram-publisher", exc)
            raise RuntimeError("Telegram returned a server error; operator paused for reconciliation") from exc
        store.finish_publication(attempt_key, "failed", error=f"HTTP {exc.code}")
        store.incident("warning", "telegram-publisher", exc)
        raise
    except Exception as exc:
        store.finish_publication(attempt_key, "uncertain", error=str(exc)[:500])
        store.set_state("paused", "1")
        store.incident("critical", "telegram-publisher", exc)
        raise RuntimeError("Telegram send outcome is uncertain; operator paused to prevent a duplicate") from exc
    store.finish_publication(attempt_key, "sent", message_id=message_id)
    store.mark_posted(row["product_key"], message_id)
    print(f"Posted {row['product_key']} as Telegram message {message_id}")
    return True


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Worth It India deals automation")
    parser.add_argument("--config", default="config.json")
    sub = parser.add_subparsers(dest="command", required=True)
    p_ingest = sub.add_parser("ingest"); p_ingest.add_argument("source")
    p_short = sub.add_parser("shortlist"); p_short.add_argument("--limit", type=int, default=20)
    sub.add_parser("next-link")
    p_link = sub.add_parser("attach-link"); p_link.add_argument("product_key"); p_link.add_argument("affiliate_url")
    p_approve = sub.add_parser("approve"); p_approve.add_argument("product_key")
    p_publish = sub.add_parser("publish-next"); p_publish.add_argument("--dry-run", action="store_true")
    p_run = sub.add_parser("run"); p_run.add_argument("source"); p_run.add_argument("--dry-run", action="store_true")
    sub.add_parser("doctor")
    p_audit = sub.add_parser("audit-tail"); p_audit.add_argument("--limit", type=int, default=20)
    p_backup = sub.add_parser("backup"); p_backup.add_argument("path", type=Path)
    p_reconcile = sub.add_parser("reconcile")
    p_reconcile.add_argument("attempt_key")
    p_reconcile.add_argument("outcome", choices=["sent", "failed"])
    p_reconcile.add_argument("--message-id")

    args = parser.parse_args(argv)
    config_path = Path(args.config).resolve()
    config, store = store_from_config(config_path)

    if args.command == "ingest":
        accepted, rejected = ingest(args.source, config, store)
        print(f"Accepted {accepted}; rejected {rejected}")
    elif args.command == "shortlist":
        print_rows(store.shortlist(args.limit))
    elif args.command == "next-link":
        rows = [r for r in store.shortlist(100) if not r["affiliate_url"]]
        if not rows:
            print("No shortlisted deal needs a link")
        else:
            r = rows[0]
            print(json.dumps({"product_key": r["product_key"], "product_url": r["product_url"], "title": r["title"]}, ensure_ascii=False))
    elif args.command == "attach-link":
        store.attach_link(args.product_key, args.affiliate_url, config["allowed_affiliate_hosts"])
        print(f"Attached affiliate link to {args.product_key}")
    elif args.command == "approve":
        store.approve(args.product_key, actor="local-admin")
        print(f"Approved {args.product_key}")
    elif args.command == "publish-next":
        publish_once(config, store, args.dry_run)
    elif args.command == "run":
        print("Worker started. Press Ctrl+C to stop.")
        owner = f"worker-{uuid.uuid4()}"
        while True:
            try:
                if store.acquire_lease("publisher", owner, max(90, int(config["scan_interval_seconds"]) * 2)):
                    ingest(args.source, config, store)
                    publish_once(config, store, args.dry_run)
                else:
                    print("Cycle skipped: another publisher owns the lease")
            except Exception as exc:
                print(f"Cycle failed: {exc}", file=sys.stderr)
            time.sleep(int(config["scan_interval_seconds"]))
    elif args.command == "doctor":
        report = store.doctor(config)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["healthy"] else 2
    elif args.command == "audit-tail":
        rows = store.db.execute(
            "SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (args.limit,)
        ).fetchall()
        print(json.dumps([dict(row) for row in rows], ensure_ascii=False, indent=2))
    elif args.command == "backup":
        destination = args.path.resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        backup_db = sqlite3.connect(destination)
        try:
            store.db.backup(backup_db)
        finally:
            backup_db.close()
        print(f"Consistent SQLite backup written to {destination}")
    elif args.command == "reconcile":
        store.reconcile_publication(args.attempt_key, args.outcome, args.message_id, "local-admin")
        print(f"Reconciled {args.attempt_key} as {args.outcome}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
