import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

import dealbot


CONFIG = {
    "freshness_minutes": 30,
    "min_discount_pct": 35,
    "min_rating_count": 50,
}


def candidate(**changes):
    values = dict(
        source="test", merchant="Myntra", product_id="123", title="Test product",
        url="https://www.myntra.com/123?utm_source=x", price=400, mrp=1000,
        category="Test", in_stock=True, observed_at=dealbot.now_utc(),
        rating_count=500, commission_pct=8,
    )
    values.update(changes)
    return dealbot.Candidate(**values)


class DealBotTests(unittest.TestCase):
    def test_score_accepts_fresh_high_discount_product(self):
        score, reason = dealbot.score_candidate(candidate(), CONFIG)
        self.assertGreater(score, 0)
        self.assertIsNone(reason)

    def test_score_rejects_sold_out_and_stale_products(self):
        self.assertEqual(dealbot.score_candidate(candidate(in_stock=False), CONFIG)[1], "sold out")
        stale = candidate(observed_at=dealbot.now_utc() - timedelta(hours=2))
        self.assertEqual(dealbot.score_candidate(stale, CONFIG)[1], "stale price")

    def test_amazon_prohibited_wording_is_rejected(self):
        item = candidate(merchant="Amazon", title="Amazon loot bug deal")
        self.assertEqual(dealbot.score_candidate(item, CONFIG)[1], "Amazon promotion wording is prohibited")

    def test_upsert_deduplicates_by_merchant_product_id(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            store = dealbot.Store(Path(tmp) / "deals.db")
            first = candidate(price=400)
            second = candidate(price=350)
            store.upsert(first, 50, None)
            store.upsert(second, 60, None)
            rows = store.shortlist()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["price"], 350)
            store.db.close()

    def test_publication_reservation_blocks_duplicate_or_uncertain_send(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            store = dealbot.Store(Path(tmp) / "deals.db")
            item = candidate()
            store.upsert(item, 80, None)
            store.attach_link(item.key, "https://myntr.it/example", ["myntr.it"])
            row = store.get(item.key)
            first = store.reserve_publication(row, "@worthit")
            self.assertIsNotNone(first)
            self.assertIsNone(store.reserve_publication(row, "@worthit"))
            store.finish_publication(first, "uncertain", error="timeout")
            self.assertIsNone(store.reserve_publication(row, "@worthit"))
            store.db.close()

    def test_approval_is_bound_to_evidence_and_material_snapshot(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            store = dealbot.Store(Path(tmp) / "deals.db")
            item = candidate()
            store.upsert(item, 80, None)
            store.attach_link(item.key, "https://myntr.it/example", ["myntr.it"])
            with self.assertRaisesRegex(ValueError, "evidence"):
                store.approve(item.key)
            store.record_evidence(
                item.key, item.url, "merchant-page-browser-check", dealbot.now_utc(),
                item.price, True, "Price and stock visible",
            )
            store.approve(item.key, actor="test-admin")
            self.assertIsNotNone(store.next_publishable({**CONFIG, "freshness_minutes": 30}))

            changed = candidate(price=350)
            store.upsert(changed, 82, None)
            self.assertEqual(store.get(item.key)["approved"], 0)
            self.assertIsNone(store.next_publishable({**CONFIG, "freshness_minutes": 30}))
            store.db.close()

    def test_link_change_resets_approval(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            store = dealbot.Store(Path(tmp) / "deals.db")
            item = candidate()
            store.upsert(item, 80, None)
            store.attach_link(item.key, "https://myntr.it/first", ["myntr.it"])
            store.record_evidence(item.key, item.url, "browser", dealbot.now_utc(), item.price, True, "ok")
            store.approve(item.key)
            store.attach_link(item.key, "https://myntr.it/second", ["myntr.it"])
            self.assertEqual(store.get(item.key)["approved"], 0)
            self.assertIsNone(store.db.execute("SELECT * FROM approvals WHERE product_key=?", (item.key,)).fetchone())
            store.db.close()

    def test_audit_chain_and_doctor(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            store = dealbot.Store(Path(tmp) / "deals.db")
            item = candidate()
            store.upsert(item, 80, None)
            self.assertTrue(store.verify_audit_chain())
            report = store.doctor({**CONFIG})
            self.assertTrue(report["healthy"])
            store.db.close()

    def test_manual_rejection_persists_until_material_change(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            store = dealbot.Store(Path(tmp) / "deals.db")
            item = candidate()
            store.upsert(item, 80, None)
            store.reject(item.key, "Not useful")
            store.upsert(item, 80, None)
            self.assertEqual(store.get(item.key)["status"], "rejected")

            store.upsert(candidate(price=350), 82, None)
            self.assertEqual(store.get(item.key)["status"], "active")
            self.assertIsNone(store.get(item.key)["manual_rejection_fingerprint"])
            store.db.close()

    def test_posted_deal_can_reenter_after_cooldown_and_new_approval_cycle(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            store = dealbot.Store(Path(tmp) / "deals.db")
            item = candidate()
            store.upsert(item, 80, None)
            store.attach_link(item.key, "https://myntr.it/example", ["myntr.it"])
            store.record_evidence(item.key, item.url, "browser", dealbot.now_utc(), item.price, True, "ok")
            store.approve(item.key)
            row = store.get(item.key)
            first_attempt = store.reserve_publication(row, "@worthit")
            store.finish_publication(first_attempt, "sent", message_id="1")
            store.mark_posted(item.key, "1")
            old = (dealbot.now_utc() - timedelta(hours=73)).isoformat()
            store.db.execute("UPDATE deals SET posted_at=? WHERE product_key=?", (old, item.key))
            store.db.commit()

            store.upsert(item, 80, None, repost_cooldown_hours=72)
            self.assertEqual(store.get(item.key)["status"], "active")
            self.assertEqual(store.get(item.key)["approved"], 0)
            store.approve(item.key)
            second_attempt = store.reserve_publication(store.get(item.key), "@worthit")
            self.assertIsNotNone(second_attempt)
            self.assertNotEqual(first_attempt, second_attempt)
            store.db.close()

    def test_amazon_price_content_is_purged_after_24_hours(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            store = dealbot.Store(Path(tmp) / "deals.db")
            item = candidate(merchant="Amazon", product_id="B0TEST", observed_at=dealbot.now_utc() - timedelta(hours=25))
            store.upsert(item, 80, None)
            store.record_evidence(item.key, item.url, "browser", item.observed_at, item.price, True, "ok")
            self.assertEqual(dealbot.expire_amazon_content(store), 1)
            row = store.get(item.key)
            self.assertEqual((row["price"], row["mrp"], row["status"]), (0, 0, "expired"))
            self.assertIsNone(store.latest_evidence(item.key))
            store.db.close()


if __name__ == "__main__":
    unittest.main()
