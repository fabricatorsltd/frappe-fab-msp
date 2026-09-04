import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fab_msp import field_service
from fab_msp.field_service import billable_hours, build_charges, charge_label


def task(**overrides):
    values = {
        "fab_fs_parte": "A26/015047",
        "fab_fs_intervention_date": "2026-09-02",
        "fab_fs_end_customer": "ROLLS ROYCE MOTOR CARS MILANO",
        "fab_fs_callout_fee": 40,
        "fab_fs_hourly_rate": 40,
        "fab_fs_hours_billed": 1.5,
    }
    values.update(overrides)
    return values


class TestBillableHours(unittest.TestCase):
    def test_seventy_minutes_round_up_to_one_and_a_half(self):
        self.assertEqual(billable_hours("10:00:00", "11:10:00"), 1.5)

    def test_exact_half_hour_is_not_rounded_up(self):
        self.assertEqual(billable_hours("10:00:00", "11:30:00"), 1.5)

    def test_a_single_minute_still_bills_half_an_hour(self):
        self.assertEqual(billable_hours("10:00:00", "10:01:00"), 0.5)

    def test_missing_time_gives_nothing(self):
        self.assertEqual(billable_hours("10:00:00", None), 0.0)
        self.assertEqual(billable_hours(None, "11:00:00"), 0.0)

    def test_same_time_gives_nothing(self):
        self.assertEqual(billable_hours("10:00:00", "10:00:00"), 0.0)

    def test_departure_before_arrival_runs_past_midnight(self):
        self.assertEqual(billable_hours("23:00:00", "00:40:00"), 2.0)


class TestChargeLabel(unittest.TestCase):
    def test_parte_date_and_end_customer(self):
        self.assertEqual(
            charge_label(task()), "A26/015047 - 02/09/2026 - ROLLS ROYCE MOTOR CARS MILANO"
        )

    def test_missing_parts_are_dropped(self):
        self.assertEqual(charge_label(task(fab_fs_end_customer=None)), "A26/015047 - 02/09/2026")


class TestBuildCharges(unittest.TestCase):
    def test_call_out_and_labour(self):
        lines = build_charges(task())
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["item"], field_service.CALLOUT_ITEM)
        self.assertEqual(lines[0]["qty"], 1)
        self.assertEqual(lines[0]["rate"], 40)
        self.assertTrue(lines[0]["description"].endswith(" - call-out"))
        self.assertEqual(lines[1]["item"], field_service.HOUR_ITEM)
        self.assertEqual(lines[1]["qty"], 1.5)
        self.assertEqual(lines[1]["rate"], 40)
        self.assertTrue(lines[1]["description"].endswith(" - labour"))

    def test_spanish_customer_gets_spanish_line_words(self):
        lines = build_charges(task(), "es")
        self.assertTrue(lines[0]["description"].endswith(" - desplazamiento"))
        self.assertTrue(lines[1]["description"].endswith(" - mano de obra"))

    def test_unknown_language_falls_back_to_english(self):
        self.assertTrue(build_charges(task(), "de")[0]["description"].endswith(" - call-out"))

    def test_no_hours_bills_the_call_out_only(self):
        lines = build_charges(task(fab_fs_hours_billed=0))
        self.assertEqual([line["item"] for line in lines], [field_service.CALLOUT_ITEM])

    def test_rates_come_from_the_task_not_the_defaults(self):
        lines = build_charges(task(fab_fs_callout_fee=60, fab_fs_hourly_rate=45))
        self.assertEqual(lines[0]["rate"], 60)
        self.assertEqual(lines[1]["rate"], 45)

    def test_empty_fee_and_rate_fall_back_to_the_defaults(self):
        lines = build_charges(task(fab_fs_callout_fee=0, fab_fs_hourly_rate=None))
        self.assertEqual(lines[0]["rate"], field_service.DEFAULT_CALLOUT_FEE)
        self.assertEqual(lines[1]["rate"], field_service.DEFAULT_HOURLY_RATE)


def doc_stub(**overrides):
    values = {"project": "PROJ-0002", **task(**overrides)}
    return SimpleNamespace(
        name="TASK-2026-00001",
        project=values["project"],
        get=values.get,
        db_set=lambda field, value=None: values.update(
            field if isinstance(field, dict) else {field: value}
        ),
        values=values,
    )


class TestBillIntervention(unittest.TestCase):
    def _frappe(self, existing, get_doc=None):
        return SimpleNamespace(
            get_all=lambda *a, **k: existing,
            get_doc=get_doc or (lambda *a, **k: self.fail("must not insert")),
            db=SimpleNamespace(
                get_value=lambda *a, **k: "TRISON EUROPE, S.L.U.", set_value=lambda *a, **k: None
            ),
        )

    def test_existing_charges_are_returned_and_nothing_is_inserted(self):
        with patch.object(field_service, "frappe", self._frappe(["MSP-CHG-0001", "MSP-CHG-0002"])), \
             patch.object(field_service, "_link_charges", lambda doc: None):
            self.assertEqual(
                field_service.bill_intervention(doc_stub()), ["MSP-CHG-0001", "MSP-CHG-0002"]
            )

    def test_two_charges_are_created_and_the_task_marked_to_bill(self):
        inserted = []

        def defer_charge(customer, item, qty, rate, description, **kwargs):
            inserted.append({"customer": customer, "item": item, "qty": qty, "rate": rate,
                             "description": description, **kwargs})
            return f"MSP-CHG-000{len(inserted)}"

        doc = doc_stub()
        with patch.object(field_service, "frappe", self._frappe([])), \
             patch.object(field_service, "_link_charges", lambda doc: None), \
             patch.dict("sys.modules", {}), \
             patch("fab_msp.fulfillment.defer_charge", defer_charge):
            names = field_service.bill_intervention(doc)
        self.assertEqual(names, ["MSP-CHG-0001", "MSP-CHG-0002"])
        self.assertEqual(
            [c["item"] for c in inserted], [field_service.CALLOUT_ITEM, field_service.HOUR_ITEM]
        )
        self.assertEqual(inserted[0]["customer"], "TRISON EUROPE, S.L.U.")
        self.assertEqual(inserted[0]["task"], "TASK-2026-00001")
        self.assertEqual(inserted[0]["posting_date"], "2026-09-02")
        self.assertEqual(doc.values["fab_fs_billing_status"], "To bill")

    def test_a_project_without_customer_bills_nothing(self):
        stub = SimpleNamespace(
            get_all=lambda *a, **k: [],
            db=SimpleNamespace(get_value=lambda *a, **k: None),
        )
        with patch.object(field_service, "frappe", stub):
            self.assertEqual(field_service.bill_intervention(doc_stub()), [])


class TestSyncUnbilledCharges(unittest.TestCase):
    def _run(self, charges, doc):
        updates = {}
        deleted = []
        inserted = []

        def get_all(doctype, filters=None, fields=None, **k):
            if fields and "status" in fields:
                return [SimpleNamespace(**c) for c in charges]
            return [SimpleNamespace(name=c["name"], item=c["item"]) for c in charges]

        stub = SimpleNamespace(
            get_all=get_all,
            delete_doc=lambda dt, name, **k: deleted.append(name),
            db=SimpleNamespace(
                get_value=lambda *a, **k: "TRISON EUROPE, S.L.U.",
                set_value=lambda dt, name, values: updates.update({name: values}),
            ),
        )

        def defer_charge(customer, item, qty, rate, description, **kwargs):
            inserted.append(item)
            return "MSP-CHG-9999"

        with patch.object(field_service, "frappe", stub), \
             patch.object(field_service, "_link_charges", lambda doc: None), \
             patch("fab_msp.fulfillment.defer_charge", defer_charge):
            field_service.sync_unbilled_charges(doc)
        return updates, deleted, inserted

    def test_unbilled_charges_follow_the_task(self):
        charges = [
            {"name": "CHG-1", "item": field_service.CALLOUT_ITEM, "status": "Unbilled"},
            {"name": "CHG-2", "item": field_service.HOUR_ITEM, "status": "Unbilled"},
        ]
        updates, deleted, inserted = self._run(
            charges, doc_stub(fab_fs_hours_billed=3, fab_fs_callout_fee=55)
        )
        self.assertEqual(updates["CHG-1"]["rate"], 55)
        self.assertEqual(updates["CHG-2"]["qty"], 3)
        self.assertEqual(deleted, [])
        self.assertEqual(inserted, [])

    def test_a_billed_charge_is_frozen(self):
        charges = [
            {"name": "CHG-1", "item": field_service.CALLOUT_ITEM, "status": "Billed"},
            {"name": "CHG-2", "item": field_service.HOUR_ITEM, "status": "Unbilled"},
        ]
        updates, _deleted, _inserted = self._run(charges, doc_stub(fab_fs_callout_fee=55))
        self.assertNotIn("CHG-1", updates)
        self.assertIn("CHG-2", updates)

    def test_hours_dropped_to_zero_removes_the_unbilled_labour_charge(self):
        charges = [
            {"name": "CHG-1", "item": field_service.CALLOUT_ITEM, "status": "Unbilled"},
            {"name": "CHG-2", "item": field_service.HOUR_ITEM, "status": "Unbilled"},
        ]
        _updates, deleted, _inserted = self._run(charges, doc_stub(fab_fs_hours_billed=0))
        self.assertEqual(deleted, ["CHG-2"])

    def test_hours_typed_after_the_close_add_the_missing_labour_charge(self):
        charges = [{"name": "CHG-1", "item": field_service.CALLOUT_ITEM, "status": "Unbilled"}]
        _updates, _deleted, inserted = self._run(charges, doc_stub(fab_fs_hours_billed=2))
        self.assertEqual(inserted, [field_service.HOUR_ITEM])

    def test_a_task_with_no_charges_is_left_alone(self):
        updates, deleted, inserted = self._run([], doc_stub())
        self.assertEqual((updates, deleted, inserted), ({}, [], []))


class TestTechnicianVisibility(unittest.TestCase):
    def _frappe(self, roles, suppliers, user="tech@x"):
        return SimpleNamespace(
            session=SimpleNamespace(user=user),
            get_roles=lambda u: roles,
            get_all=lambda *a, **k: suppliers,
            db=SimpleNamespace(escape=lambda v: f"'{v}'"),
        )

    def test_own_supplier_narrows_the_list(self):
        with patch.object(field_service, "frappe", self._frappe(["Field Technician"], ["SUP-A"])):
            self.assertEqual(
                field_service.task_query_conditions(),
                "(`tabTask`.`fab_fs_supplier` in ('SUP-A'))",
            )

    def test_without_a_supplier_permission_the_list_falls_back_to_the_user(self):
        with patch.object(field_service, "frappe", self._frappe(["Field Technician"], [])):
            self.assertEqual(
                field_service.task_query_conditions("tech@x"),
                "(`tabTask`.`fab_fs_technician_user` = 'tech@x')",
            )

    def test_an_office_role_is_not_narrowed(self):
        with patch.object(
            field_service, "frappe", self._frappe(["Field Technician", "Projects Manager"], ["SUP-A"])
        ):
            self.assertEqual(field_service.task_query_conditions(), "")

    def test_administrator_is_not_narrowed(self):
        with patch.object(field_service, "frappe", self._frappe(["Field Technician"], ["SUP-A"])):
            self.assertEqual(field_service.task_query_conditions("Administrator"), "")

    def test_own_task_is_permitted_and_another_supplier_is_not(self):
        with patch.object(field_service, "frappe", self._frappe(["Field Technician"], ["SUP-A"])):
            self.assertTrue(field_service.task_has_permission(Doc({"fab_fs_supplier": "SUP-A"})))
            self.assertFalse(field_service.task_has_permission(Doc({"fab_fs_supplier": "SUP-B"})))
            self.assertFalse(field_service.task_has_permission(Doc({})))

    def test_a_task_without_supplier_matches_only_its_technician_user(self):
        with patch.object(field_service, "frappe", self._frappe(["Field Technician"], [])):
            self.assertTrue(
                field_service.task_has_permission(Doc({"fab_fs_technician_user": "tech@x"}))
            )
            self.assertFalse(field_service.task_has_permission(Doc({})))


class Doc(dict):
    """The smallest thing has_permission touches: a doc with .get()."""


if __name__ == "__main__":
    unittest.main()
