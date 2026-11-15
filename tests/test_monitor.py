"""Tests for the acceptance criteria in PRD.md.

Run with:  python -m unittest discover tests
"""
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import monitor as m  # noqa: E402

NOW = datetime(2026, 11, 12, 10, 0)  # Thursday


def order(**overrides):
    """A healthy order placed two hours ago. Override fields per test."""
    o = dict(
        order_id="RX-TEST", placed_at=NOW - timedelta(hours=2),
        clinic="Test Clinic", pet_parent="Sam", pet_name="Biscuit", species="Dog",
        item="Methimazole 5 mg tablets", item_type="rx_med", urgency="critical",
        partner="Test Pharmacy", is_autoship=False,
        promised_by=(NOW + timedelta(days=5)).date(),
        approved_at=None, stock_status="in_stock", stock_issue_at=None,
        customer_notified_at=None, label_created_at=None, first_scan_at=None,
        last_scan_at=None, delivered_at=None, cancelled_at=None, support_contacts=0,
    )
    o.update(overrides)
    return o


def subscription(**overrides):
    s = dict(
        subscription_id="SUB-TEST", clinic="Test Clinic", clinic_status="active",
        pet_parent="Sam", pet_name="Biscuit", item="Renal support diet, 12 cans",
        urgency="standard", next_ship_date=(NOW + timedelta(days=2)).date(),
        refills_remaining=3, address_changed_at=None, address_verified=True,
    )
    s.update(overrides)
    return s


class BusinessClock(unittest.TestCase):
    def test_weekend_hours_do_not_count(self):
        friday_5pm = datetime(2026, 11, 6, 17, 0)
        monday_9am = datetime(2026, 11, 9, 9, 0)
        self.assertEqual(m.business_hours_between(friday_5pm, monday_9am), 16)

    def test_weekday_hours_count_in_full(self):
        self.assertEqual(
            m.business_hours_between(datetime(2026, 11, 10, 8), datetime(2026, 11, 11, 8)), 24)


class StageClocks(unittest.TestCase):
    def test_fresh_order_is_not_flagged(self):
        self.assertIsNone(m.evaluate_order(order(), NOW))

    def test_slow_vet_approval_is_flagged(self):
        r = m.evaluate_order(order(placed_at=NOW - timedelta(hours=30)), NOW)
        self.assertEqual(r["stage"], "AWAITING_APPROVAL")
        self.assertEqual(r["owner"], "Clinic")
        self.assertIsNotNone(r["message_clinic"])

    def test_approved_but_not_shipped_is_flagged(self):
        o = order(placed_at=NOW - timedelta(days=3), approved_at=NOW - timedelta(hours=40))
        self.assertEqual(m.evaluate_order(o, NOW)["root_cause"], "Fulfillment backlog")

    def test_compounded_items_get_a_longer_fulfillment_clock(self):
        o = order(placed_at=NOW - timedelta(days=3), approved_at=NOW - timedelta(hours=40),
                  item_type="compounded")
        self.assertIsNone(m.evaluate_order(o, NOW))

    def test_stockout_is_flagged_immediately(self):
        o = order(approved_at=NOW - timedelta(hours=1), stock_status="out_of_stock",
                  stock_issue_at=NOW - timedelta(minutes=10))
        self.assertEqual(m.evaluate_order(o, NOW)["root_cause"], "Stockout, customer not told")

    def test_label_without_scan_is_not_called_shipped(self):
        o = order(placed_at=NOW - timedelta(days=3), approved_at=NOW - timedelta(days=2, hours=12),
                  label_created_at=NOW - timedelta(hours=30))
        r = m.evaluate_order(o, NOW)
        self.assertEqual(r["root_cause"], "Carrier pickup missed")
        self.assertEqual(r["customer_status"], "Preparing shipment")
        self.assertNotEqual(m.customer_status(o), "Shipped")

    def test_shipped_status_requires_a_carrier_scan(self):
        o = order(approved_at=NOW - timedelta(hours=20), label_created_at=NOW - timedelta(hours=10),
                  first_scan_at=NOW - timedelta(hours=4), last_scan_at=NOW - timedelta(hours=4))
        self.assertEqual(m.customer_status(o), "Shipped")
        self.assertIsNone(m.evaluate_order(o, NOW))

    def test_stalled_tracking_is_flagged(self):
        o = order(placed_at=NOW - timedelta(days=4), approved_at=NOW - timedelta(days=3, hours=20),
                  label_created_at=NOW - timedelta(days=3, hours=12),
                  first_scan_at=NOW - timedelta(days=3, hours=6),
                  last_scan_at=NOW - timedelta(hours=60))
        self.assertEqual(m.evaluate_order(o, NOW)["root_cause"], "Carrier tracking stalled")

    def test_past_promised_date_is_flagged(self):
        o = order(placed_at=NOW - timedelta(days=7), promised_by=date(2026, 11, 10),
                  approved_at=NOW - timedelta(days=6, hours=20),
                  label_created_at=NOW - timedelta(days=6),
                  first_scan_at=NOW - timedelta(days=5, hours=12),
                  last_scan_at=NOW - timedelta(hours=5))
        self.assertEqual(m.evaluate_order(o, NOW)["root_cause"],
                         "Carrier delay past promised date")

    def test_late_because_it_shipped_late_is_not_blamed_on_the_carrier(self):
        o = order(placed_at=NOW - timedelta(days=7), promised_by=date(2026, 11, 10),
                  approved_at=NOW - timedelta(days=6), label_created_at=NOW - timedelta(hours=8),
                  first_scan_at=NOW - timedelta(hours=2), last_scan_at=NOW - timedelta(hours=2))
        r = m.evaluate_order(o, NOW)
        self.assertEqual(r["root_cause"], "Shipped too late to meet promise")
        self.assertEqual(r["owner"], "Support")

    def test_delivered_and_cancelled_orders_are_never_flagged(self):
        self.assertIsNone(m.evaluate_order(order(delivered_at=NOW, approved_at=NOW,
                                                 label_created_at=NOW, first_scan_at=NOW), NOW))
        self.assertIsNone(m.evaluate_order(order(placed_at=NOW - timedelta(days=9),
                                                 cancelled_at=NOW), NOW))


class Queue(unittest.TestCase):
    def test_critical_medications_sort_first(self):
        standard = order(order_id="A", urgency="standard", placed_at=NOW - timedelta(days=6))
        critical = order(order_id="B", urgency="critical", placed_at=NOW - timedelta(hours=30))
        queue = m.build_queue([standard, critical], NOW)
        self.assertEqual([r["order_id"] for r in queue], ["B", "A"])

    def test_every_flagged_order_has_a_drafted_message_and_action(self):
        orders = [order(order_id=str(i), placed_at=NOW - timedelta(hours=30 + i)) for i in range(5)]
        for r in m.build_queue(orders, NOW):
            self.assertTrue(r["message_pet_parent"])
            self.assertTrue(r["action"])


class Autoship(unittest.TestCase):
    def test_healthy_subscription_ships(self):
        self.assertIsNone(m.evaluate_subscription(subscription(), NOW))

    def test_closed_clinic_pauses_autoship(self):
        r = m.evaluate_subscription(subscription(clinic_status="closed"), NOW)
        self.assertEqual(r["issue"], "Clinic no longer active")

    def test_no_refills_pauses_autoship(self):
        r = m.evaluate_subscription(subscription(refills_remaining=0), NOW)
        self.assertEqual(r["issue"], "No refills remaining")

    def test_unverified_address_change_pauses_autoship(self):
        r = m.evaluate_subscription(subscription(
            address_changed_at=NOW - timedelta(days=3), address_verified=False), NOW)
        self.assertEqual(r["issue"], "Address changed recently, not verified")

    def test_subscriptions_outside_the_lookahead_are_not_checked_yet(self):
        s = subscription(clinic_status="closed", next_ship_date=(NOW + timedelta(days=10)).date())
        self.assertIsNone(m.evaluate_subscription(s, NOW))


class SampleData(unittest.TestCase):
    """Run the engine over the included sample data."""

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent.parent
        cls.orders = m.load_orders(root / "data" / "orders.csv")
        cls.queue = m.build_queue(cls.orders, NOW)

    def test_every_open_order_has_one_known_stage(self):
        known = set(m.STAGE_LABEL) | {"DELIVERED", "CANCELLED"}
        for o in self.orders:
            self.assertIn(m.stage_of(o), known)

    def test_every_queue_record_is_complete(self):
        self.assertGreater(len(self.queue), 0)
        for r in self.queue:
            for field in ("stage_label", "in_stage_text", "root_cause", "owner",
                          "action", "message_pet_parent", "customer_status"):
                self.assertTrue(r[field], f"{r['order_id']} is missing {field}")


if __name__ == "__main__":
    unittest.main()
