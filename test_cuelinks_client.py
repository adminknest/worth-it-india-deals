import json
import tempfile
import unittest
from pathlib import Path

from cuelinks_client import AffiliateStore, PolicyDecision, policy_decision


class PolicyTests(unittest.TestCase):
    def test_telegram_needs_review_even_when_social_is_allowed(self):
        campaign = {"access_status": "open", "social_media_publishers_allowed": True, "media": {"disallowed": []}}
        self.assertEqual(policy_decision(campaign, "telegram"), PolicyDecision(False, "campaign requires one-time merchant policy review"))

    def test_social_disallow_cannot_be_bypassed_without_review(self):
        campaign = {"access_status": "open", "social_media_publishers_allowed": False}
        self.assertFalse(policy_decision(campaign, "telegram", "allow").allowed)

    def test_reviewed_allowed_campaign_passes(self):
        campaign = {"access_status": "approved", "social_media_publishers_allowed": True, "media": {"disallowed": []}}
        self.assertTrue(policy_decision(campaign, "telegram", "allow").allowed)


class StoreTests(unittest.TestCase):
    def test_transaction_upsert_is_idempotent(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            store = AffiliateStore(Path(directory) / "affiliate.db")
            first = {"id": 1, "campaign_id": 9, "status": "pending", "user_commission": "10.00", "updated_at": "2026-01-01T00:00:00Z"}
            second = {**first, "status": "validated", "user_commission": "12.50", "updated_at": "2026-01-02T00:00:00Z"}
            store.upsert_transaction(first)
            store.upsert_transaction(second)
            store.db.commit()
            rows = store.db.execute("SELECT status, commission FROM transactions").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0]["status"], rows[0]["commission"]), ("validated", 12.5))
            store.db.close()


if __name__ == "__main__":
    unittest.main()
