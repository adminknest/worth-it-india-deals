import argparse
import csv
import tempfile
import unittest
from pathlib import Path

import dealbot
import earnkaro_intake


CONFIG = {
    "freshness_minutes": 30,
    "min_discount_pct": 35,
    "min_rating_count": 50,
    "allowed_affiliate_hosts": ["bitli.in", "myntr.it", "earnkaro.com"],
}


class EarnKaroIntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.store = dealbot.Store(Path(self.tmp.name) / "deals.db")
        earnkaro_intake.ensure_schema(self.store)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_verified_record_stays_pending_for_admin(self):
        args = argparse.Namespace(
            merchant="Amazon", product_id="B0TEST", title="Verified item",
            merchant_page_url="https://amazon.in/dp/B0TEST",
            earnkaro_source_url="https://earnkaro.com/example/PPS1-Amazon",
            affiliate_url="https://bitli.in/example", price=699, mrp=3499,
            category="Health", rating_count=500, commission_pct=5.5,
            stock_verified=True, verification_note="Price and stock visible",
            terms_snapshot="Tracking 48h; confirmation 75d",
        )
        key = earnkaro_intake.record_verified_deal(args, CONFIG, self.store)
        row = self.store.get(key)
        self.assertEqual(row["approved"], 0)
        self.assertEqual(row["affiliate_url"], "https://bitli.in/example")
        self.assertEqual(row["status"], "active")

    def test_unverified_stock_is_rejected(self):
        args = argparse.Namespace(stock_verified=False)
        with self.assertRaisesRegex(ValueError, "stock"):
            earnkaro_intake.record_verified_deal(args, CONFIG, self.store)

    def test_report_import_is_idempotent(self):
        report = Path(self.tmp.name) / "report.csv"
        with report.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=["event_id", "occurred_at", "merchant", "order_amount", "commission_amount", "status"])
            writer.writeheader()
            writer.writerow({"event_id":"1", "occurred_at":"2026-10-20T10:00:00+00:00", "merchant":"Amazon", "order_amount":"699", "commission_amount":"38.45", "status":"pending"})
        self.assertEqual(earnkaro_intake.import_report(report, self.store), (1, 0))
        self.assertEqual(earnkaro_intake.import_report(report, self.store), (0, 1))


if __name__ == "__main__":
    unittest.main()
