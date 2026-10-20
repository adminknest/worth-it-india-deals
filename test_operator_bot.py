import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dealbot
import operator_bot


CONFIG = {
    "freshness_minutes": 30,
    "min_discount_pct": 35,
    "min_rating_count": 50,
    "allowed_affiliate_hosts": ["bitli.in", "myntr.it", "earnkaro.com"],
    "repost_cooldown_hours": 72,
}


class OperatorBotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.store = dealbot.Store(Path(self.tmp.name) / "deals.db")

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    @mock.patch.dict(os.environ, {"TG_ADMIN_USER_ID": "42"}, clear=False)
    @mock.patch("operator_bot.reply")
    def test_unauthorized_user_is_ignored(self, reply):
        operator_bot.handle_command("/status", 100, 7, CONFIG, self.store)
        reply.assert_not_called()

    @mock.patch.dict(os.environ, {"TG_ADMIN_USER_ID": "42"}, clear=False)
    @mock.patch("operator_bot.reply")
    def test_admin_can_record_verified_earnkaro_deal(self, reply):
        payload = {
            "merchant": "Amazon", "product_id": "B0TEST", "title": "Verified item",
            "merchant_page_url": "https://amazon.in/dp/B0TEST",
            "earnkaro_source_url": "https://earnkaro.com/example/PPS1-Amazon",
            "affiliate_url": "https://bitli.in/example", "price": 699, "mrp": 3499,
            "category": "Health", "rating_count": 500, "commission_pct": 5.5,
            "verification_note": "Price and stock visible",
            "terms_snapshot": "Tracking 48h; confirmation 75d",
        }
        operator_bot.handle_command("/record " + json.dumps(payload), 100, 42, CONFIG, self.store)
        row = self.store.get("amazon:b0test")
        self.assertEqual(row["approved"], 0)
        self.assertIsNotNone(self.store.latest_evidence("amazon:b0test"))
        reply.assert_called_once()

    @mock.patch.dict(os.environ, {"TG_ADMIN_USER_ID": "42"}, clear=False)
    @mock.patch("operator_bot.reply")
    def test_empty_queue_alert_is_rate_limited(self, reply):
        config = dict(CONFIG, queue_starvation_alert_minutes=0, queue_starvation_repeat_minutes=360)
        self.assertTrue(operator_bot.maybe_alert_queue_starvation(config, self.store))
        self.assertFalse(operator_bot.maybe_alert_queue_starvation(config, self.store))
        reply.assert_called_once()

    @mock.patch.dict(os.environ, {"TG_CHANNEL_ID": "@WorthItIndiaDeals"}, clear=False)
    @mock.patch("operator_bot.api_call", return_value=2)
    def test_growth_snapshot_records_subscribers(self, api_call):
        snapshot = operator_bot.record_growth_snapshot(CONFIG, self.store, force=True)
        self.assertEqual(snapshot["subscriber_count"], 2)
        saved = self.store.db.execute("SELECT * FROM growth_snapshots").fetchone()
        self.assertEqual(saved["subscriber_count"], 2)
        api_call.assert_called_once_with("getChatMemberCount", {"chat_id": "@WorthItIndiaDeals"})


if __name__ == "__main__":
    unittest.main()
