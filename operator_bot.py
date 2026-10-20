#!/usr/bin/env python3
"""Private Telegram admin controls for the Worth It India publisher."""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from dealbot import expire_amazon_content, format_post, ingest, publish_once, store_from_config
from earnkaro_intake import ensure_schema as ensure_earnkaro_schema
from earnkaro_intake import record_verified_deal


def api_call(method: str, payload: dict | None = None) -> dict | list:
    token = os.environ.get("TG_BOT_TOKEN")
    if not token:
        raise RuntimeError("TG_BOT_TOKEN is required")
    url = f"https://api.telegram.org/bot{token}/{method}"
    request = urllib.request.Request(url, data=urllib.parse.urlencode(payload or {}).encode(), method="POST")
    with urllib.request.urlopen(request, timeout=40) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError(result.get("description", "Telegram API error"))
    return result["result"]


def reply(chat_id: int, text: str) -> None:
    api_call("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML"})


def status_text(store) -> str:
    queued = store.db.execute(
        """SELECT COUNT(*) FROM deals d JOIN approvals a ON a.product_key=d.product_key
           WHERE d.status='active' AND d.approved=1 AND d.affiliate_url IS NOT NULL
             AND a.deal_fingerprint=d.deal_fingerprint"""
    ).fetchone()[0]
    pending = store.db.execute("SELECT COUNT(*) FROM deals WHERE status='active' AND approved=0").fetchone()[0]
    posted = store.db.execute("SELECT COUNT(*) FROM deals WHERE status='posted'").fetchone()[0]
    uncertain = store.db.execute("SELECT COUNT(*) FROM publication_attempts WHERE state='uncertain'").fetchone()[0]
    return (
        f"<b>Worth It India operator</b>\n"
        f"Paused: {'yes' if store.state('paused', '0') == '1' else 'no'}\n"
        f"Pending review: {pending}\nQueued: {queued}\nPosted: {posted}\nUncertain sends: {uncertain}\n"
        f"Audit chain: {'valid' if store.verify_audit_chain() else 'BROKEN'}"
    )


def handle_command(text: str, chat_id: int, user_id: int, config: dict, store) -> None:
    configured = os.environ.get("TG_ADMIN_USER_ID", "")
    if not configured or str(user_id) != configured:
        return
    parts = text.strip().split(maxsplit=2)
    command = parts[0].split("@", 1)[0].lower()
    raw_payload = text.strip().partition(" ")[2].strip()
    if command in {"/start", "/help"}:
        reply(chat_id, "Commands: /status, /doctor, /queue, /preview KEY, /record JSON, /approve KEY, /reject KEY, /publish, /pause, /resume")
    elif command == "/status":
        reply(chat_id, status_text(store))
    elif command == "/doctor":
        report = store.doctor(config)
        state = "healthy" if report["healthy"] else "needs attention"
        reply(chat_id, f"Pipeline: <b>{state}</b>\n<code>{html.escape(json.dumps(report, sort_keys=True))}</code>")
    elif command == "/queue":
        rows = store.shortlist(8)
        if not rows:
            reply(chat_id, "No active candidates.")
        else:
            lines = [f"<code>{html.escape(r['product_key'])}</code> · ₹{r['price']:.0f} · {html.escape(r['title'][:80])}" for r in rows]
            reply(chat_id, "<b>Top candidates</b>\n" + "\n".join(lines))
    elif command == "/preview" and len(parts) >= 2:
        row = store.get(parts[1])
        reply(chat_id, "<b>Preview</b>\n\n" + format_post(row))
    elif command == "/record" and raw_payload:
        try:
            data = json.loads(raw_payload)
            required = {
                "merchant", "product_id", "title", "merchant_page_url", "earnkaro_source_url",
                "affiliate_url", "price", "mrp", "verification_note", "terms_snapshot",
            }
            missing = sorted(required - data.keys())
            if missing:
                raise ValueError("Missing fields: " + ", ".join(missing))
            record_args = argparse.Namespace(
                merchant=str(data["merchant"]), product_id=str(data["product_id"]),
                title=str(data["title"]), merchant_page_url=str(data["merchant_page_url"]),
                earnkaro_source_url=str(data["earnkaro_source_url"]), affiliate_url=str(data["affiliate_url"]),
                price=float(data["price"]), mrp=float(data["mrp"]),
                category=str(data.get("category", "General")), rating_count=int(data.get("rating_count", 0)),
                commission_pct=float(data.get("commission_pct", 0)), stock_verified=True,
                verification_note=str(data["verification_note"]), terms_snapshot=str(data["terms_snapshot"]),
            )
            ensure_earnkaro_schema(store)
            key = record_verified_deal(record_args, config, store)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            reply(chat_id, f"Record blocked: {html.escape(str(exc))}")
        else:
            reply(chat_id, f"Recorded <code>{html.escape(key)}</code>. Review with /preview, then /approve.")
    elif command == "/approve" and len(parts) >= 2:
        try:
            store.approve(parts[1], actor=f"telegram-admin:{user_id}")
        except (KeyError, ValueError) as exc:
            reply(chat_id, f"Approval blocked: {html.escape(str(exc))}")
        else:
            reply(chat_id, f"Approved <code>{html.escape(parts[1])}</code>.")
    elif command == "/reject" and len(parts) >= 2:
        reason = parts[2] if len(parts) == 3 else "rejected by administrator"
        store.reject(parts[1], reason)
        reply(chat_id, f"Rejected <code>{html.escape(parts[1])}</code>.")
    elif command == "/publish":
        posted = publish_once(config, store, dry_run=False)
        reply(chat_id, "Published the next approved deal." if posted else "Nothing was published; check /status.")
    elif command == "/pause":
        store.set_state("paused", "1")
        reply(chat_id, "Publishing paused.")
    elif command == "/resume":
        store.set_state("paused", "0")
        reply(chat_id, "Publishing resumed.")
    else:
        reply(chat_id, "Unknown or incomplete command. Use /help.")


def poll_once(config: dict, store) -> int:
    offset = int(store.state("telegram_update_offset", "0"))
    updates = api_call("getUpdates", {"offset": offset, "timeout": 20, "allowed_updates": json.dumps(["message"])})
    for update in updates:
        offset = max(offset, int(update["update_id"]) + 1)
        message = update.get("message") or {}
        text = message.get("text", "")
        if text.startswith("/"):
            handle_command(text, int(message["chat"]["id"]), int(message.get("from", {}).get("id", 0)), config, store)
    store.set_state("telegram_update_offset", str(offset))
    return len(updates)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Worth It India Telegram operator")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--source", help="Authorized CSV/JSON feed to ingest each cycle")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--auto-publish", action="store_true", help="publish approved deals when eligible")
    args = parser.parse_args(argv)
    config, store = store_from_config(Path(args.config).resolve())
    owner = f"operator-{uuid.uuid4()}"
    last_ingest = 0.0
    last_publish_check = 0.0
    while True:
        try:
            if not store.acquire_lease("telegram-operator", owner, 60):
                print("operator cycle skipped: another operator owns the lease", file=sys.stderr)
                time.sleep(5)
                continue
            if args.source and time.monotonic() - last_ingest >= int(config["scan_interval_seconds"]):
                ingest(args.source, config, store)
                last_ingest = time.monotonic()
            expire_amazon_content(store)
            poll_once(config, store)
            if args.auto_publish and time.monotonic() - last_publish_check >= int(config["scan_interval_seconds"]):
                publish_once(config, store, dry_run=False)
                last_publish_check = time.monotonic()
        except Exception as exc:
            store.incident("warning", "telegram-operator", exc)
            print(f"operator cycle failed: {exc}", file=sys.stderr)
        if args.once:
            return 0
        time.sleep(2)


if __name__ == "__main__":
    raise SystemExit(main())
