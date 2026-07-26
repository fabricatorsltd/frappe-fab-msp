import unittest

from fab_msp.billing import build_consolidated_items

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


if __name__ == "__main__":
    unittest.main()
