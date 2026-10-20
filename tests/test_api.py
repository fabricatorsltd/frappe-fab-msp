import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fab_msp import api, fulfillment


def ticket(ticket_type="T", customer="CUST"):
    return SimpleNamespace(ticket_type=ticket_type, customer=customer, name="TKT")


class TestTicketTypeRules(unittest.TestCase):
    def test_reads_type_values(self):
        stub = SimpleNamespace(db=SimpleNamespace(get_value=lambda dt, n, f: ("Customer Manager", "Create")))
        with patch.object(api, "frappe", stub):
            self.assertEqual(api._ticket_type_rules(ticket()), ("Customer Manager", "Create"))

    def test_defaults_without_type(self):
        with patch.object(api, "frappe", SimpleNamespace()):
            self.assertEqual(api._ticket_type_rules(ticket(ticket_type=None)), ("Internal Manager", "None"))


class TestCanApprove(unittest.TestCase):
    def _stub(self, roles):
        return SimpleNamespace(get_roles=lambda user: roles, db=SimpleNamespace())

    def test_customer_manager_route_allows_only_customer_manager(self):
        with patch.object(api, "_ticket_type_rules", return_value=("Customer Manager", "Create")), \
             patch.object(api, "_is_customer_manager", return_value=True), \
             patch.object(api, "frappe", self._stub([])):
            self.assertTrue(api._can_approve(ticket(), "boss@x"))

    def test_customer_manager_route_blocks_non_manager(self):
        with patch.object(api, "_ticket_type_rules", return_value=("Customer Manager", "Create")), \
             patch.object(api, "_is_customer_manager", return_value=False), \
             patch.object(api, "frappe", self._stub(["System Manager"])):
            # even a System Manager cannot approve a customer-manager request
            self.assertFalse(api._can_approve(ticket(), "staff@x"))

    def test_internal_route_allows_internal_role(self):
        with patch.object(api, "_ticket_type_rules", return_value=("Internal Manager", "None")), \
             patch.object(api, "frappe", self._stub(["Agent Manager"])):
            self.assertTrue(api._can_approve(ticket(), "agent@x"))

    def test_internal_route_blocks_without_role(self):
        with patch.object(api, "_ticket_type_rules", return_value=("Internal Manager", "None")), \
             patch.object(api, "frappe", self._stub(["Agent"])):
            self.assertFalse(api._can_approve(ticket(), "agent@x"))


def identity(text, *args, **kwargs):
    return text


class TestStatusForCategory(unittest.TestCase):
    STATUSES = [
        {"name": "Resolved", "category": "Resolved", "order": 3},
        {"name": "Replied", "category": "Paused", "order": 2},
        {"name": "On hold", "category": "Paused", "order": 5},
        {"name": "Open", "category": "Open", "order": 1},
    ]

    def test_first_status_of_the_category_by_order(self):
        self.assertEqual(api._status_for_category(self.STATUSES, "Paused"), "Replied")
        self.assertEqual(api._status_for_category(self.STATUSES, "Open"), "Open")

    def test_unordered_status_comes_last(self):
        statuses = [{"name": "A", "category": "Paused", "order": None}, *self.STATUSES]
        self.assertEqual(api._status_for_category(statuses, "Paused"), "Replied")

    def test_none_without_the_category(self):
        self.assertIsNone(api._status_for_category(self.STATUSES[:1], "Paused"))


class TestProrataUnit(unittest.TestCase):
    def setUp(self):
        # frappe's flt reads the site's rounding method
        rounding = patch.object(fulfillment, "flt", lambda value, precision=None: round(value, precision))
        rounding.start()
        self.addCleanup(rounding.stop)

    def test_rate_over_the_days_to_renewal(self):
        # 42.5 a year, 77 days to 2027-01-05
        self.assertEqual(fulfillment.prorata_unit(42.5, "2027-01-05", "2026-10-20"), (8.97, 77))

    def test_past_renewal_charges_nothing(self):
        self.assertEqual(fulfillment.prorata_unit(42.5, "2026-01-05", "2026-10-20"), (0.0, 0))


class TestCostLines(unittest.TestCase):
    def lines(self, **quote):
        with patch.object(api, "_", identity):
            return api._cost_lines(quote, lambda amount: f"EUR {amount:.2f}", lambda d: f"[{d}]")

    def test_prorata_until_renewal_then_annual_rate(self):
        self.assertEqual(
            self.lines(kind="prorata", total=8.97, rate=42.5, interval="Annual", renewal_date="2027-01-05"),
            ["EUR 8.97 now, pro-rata until the [2027-01-05] renewal", "(EUR 42.50 per licence per year after that)"],
        )

    def test_monthly_rate(self):
        lines = self.lines(kind="prorata", total=1, rate=4, interval="Monthly", renewal_date="2027-01-05")
        self.assertEqual(lines[1], "(EUR 4.00 per licence per month after that)")

    def test_one_time(self):
        self.assertEqual(self.lines(kind="one_time", total=85), ["EUR 85.00, one-time charge"])

    def test_no_price_shows_no_cost(self):
        self.assertEqual(self.lines(kind=None), [])


class TestChangeText(unittest.TestCase):
    def test_quantities(self):
        with patch.object(api, "_", identity):
            self.assertEqual(api._change_text({"qty": 1, "current_qty": 47}), "+1 licence (47 -> 48)")
            self.assertEqual(api._change_text({"qty": 3, "current_qty": 47}), "+3 licences (47 -> 50)")
            self.assertEqual(api._change_text({"qty": 2, "current_qty": None}), "+2 licences")


if __name__ == "__main__":
    unittest.main()
