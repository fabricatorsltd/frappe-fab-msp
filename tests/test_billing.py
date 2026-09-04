import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fab_msp import billing
from fab_msp.billing import annual_pools_billed, build_consolidated_items

RATE = {"EXO-P1": 40.0, "ACRONIS": 5.0}


def rate_fn(item):
    return RATE.get(item, 0)


def line_for(items, item_code):
    return next((i for i in items if i["item_code"] == item_code), None)


class TestConsolidatedLines(unittest.TestCase):
    def _pools(self):
        return [
            {"name": "CS-ACR", "billing_item": "ACRONIS", "quantity": 10,
             "service_label": "Acronis", "billing_interval": "Monthly", "renewal_date": None},
            {"name": "CS-M365", "billing_item": "EXO-P1", "quantity": 55,
             "service_label": "M365", "billing_interval": "Annual", "renewal_date": "2027-01-28"},
        ]

    def test_monthly_bills_every_month_annual_only_in_renewal_month(self):
        # July: monthly present, annual absent (renewal is January)
        items = build_consolidated_items("2026-07-31", self._pools(), [], rate_fn)
        self.assertIsNotNone(line_for(items, "ACRONIS"))
        self.assertIsNone(line_for(items, "EXO-P1"))
        self.assertEqual(line_for(items, "ACRONIS")["qty"], 10)
        self.assertEqual(line_for(items, "ACRONIS")["rate"], 5.0)

    def test_annual_billed_in_renewal_month(self):
        items = build_consolidated_items("2027-01-31", self._pools(), [], rate_fn)
        annual = line_for(items, "EXO-P1")
        self.assertIsNotNone(annual)
        self.assertEqual(annual["qty"], 55)
        self.assertEqual(annual["rate"], 40.0)
        self.assertIn("annual 2027", annual["description"])

    def test_addition_excluded_from_base_no_double_count(self):
        charges = [
            {"item": "EXO-P1", "qty": 2, "rate": 20.38,
             "description": "Pro-rata to 2027-01-28", "customer_service": "CS-M365"}
        ]
        # renewal month so the annual base would show; base must drop the 2 added
        items = build_consolidated_items("2027-01-31", self._pools(), charges, rate_fn)
        base = line_for(items, "EXO-P1")  # first EXO-P1 line is the base
        self.assertEqual(base["qty"], 53)  # 55 - 2
        addition = [i for i in items if i["item_code"] == "EXO-P1"][1]
        self.assertEqual(addition["qty"], 2)
        self.assertEqual(addition["rate"], 20.38)

    def test_addition_present_even_without_base(self):
        charges = [
            {"item": "EXO-P1", "qty": 2, "rate": 20.38,
             "description": "Pro-rata", "customer_service": "CS-M365"}
        ]
        # July: no annual base, but the addition line is billed
        items = build_consolidated_items("2026-07-31", self._pools(), charges, rate_fn)
        exo = [i for i in items if i["item_code"] == "EXO-P1"]
        self.assertEqual(len(exo), 1)
        self.assertEqual(exo[0]["qty"], 2)

    def test_pool_fully_moved_to_additions_has_no_base_line(self):
        charges = [{"item": "ACRONIS", "qty": 10, "rate": 5.0,
                    "description": "x", "customer_service": "CS-ACR"}]
        items = build_consolidated_items("2026-07-31", self._pools(), charges, rate_fn)
        acr_lines = [i for i in items if i["item_code"] == "ACRONIS"]
        # base excluded (10-10=0), only the addition line remains
        self.assertEqual(len(acr_lines), 1)
        self.assertEqual(acr_lines[0]["qty"], 10)

    def test_monthly_lines_come_before_annual_whatever_the_pool_order(self):
        pools = list(reversed(self._pools()))  # annual first as given
        items = build_consolidated_items("2027-01-31", pools, [], rate_fn)
        self.assertEqual([i["item_code"] for i in items], ["ACRONIS", "EXO-P1"])

    def test_annual_year_comes_from_the_billed_period_not_the_renewal_date(self):
        pools = self._pools()
        pools[1]["renewal_date"] = "2026-01-05"  # stale, a year behind
        items = build_consolidated_items("2027-01-05", pools, [], rate_fn)
        self.assertIn("annual 2027", line_for(items, "EXO-P1")["description"])


class TestAnnualPoolsBilled(unittest.TestCase):
    def _pools(self):
        return [
            {"name": "CS-ACR", "billing_item": "ACRONIS", "quantity": 10,
             "service_label": "Acronis", "billing_interval": "Monthly", "renewal_date": None},
            {"name": "CS-M365", "billing_item": "EXO-P1", "quantity": 55,
             "service_label": "M365", "billing_interval": "Annual", "renewal_date": "2027-01-28"},
        ]

    def test_annual_pool_billed_in_its_renewal_month(self):
        self.assertEqual(annual_pools_billed("2027-01-31", self._pools(), []), ["CS-M365"])

    def test_nothing_outside_the_renewal_month(self):
        self.assertEqual(annual_pools_billed("2026-07-31", self._pools(), []), [])

    def test_pool_fully_moved_to_additions_is_not_billed(self):
        charges = [{"item": "EXO-P1", "qty": 55, "rate": 40.0,
                    "description": "x", "customer_service": "CS-M365"}]
        self.assertEqual(annual_pools_billed("2027-01-31", self._pools(), charges), [])


class TestDuplicateGuard(unittest.TestCase):
    def test_existing_invoice_for_the_period_is_returned_untouched(self):
        seen = {}

        def get_value(doctype, filters, field):
            seen.update(doctype=doctype, filters=filters, field=field)
            return "FATT/2026/00037"

        stub = SimpleNamespace(db=SimpleNamespace(get_value=get_value))
        with patch.object(billing, "_erp_customer", return_value="RYI"), \
             patch.object(billing, "frappe", stub):
            self.assertEqual(billing.generate_for_customer("RYI", "2026-09-30"), "FATT/2026/00037")
        self.assertEqual(seen["doctype"], "Sales Invoice")
        self.assertEqual(seen["filters"]["remarks"], "MSP consolidated invoice 2026-09")
        self.assertEqual(seen["filters"]["docstatus"], ["<", 2])


if __name__ == "__main__":
    unittest.main()
