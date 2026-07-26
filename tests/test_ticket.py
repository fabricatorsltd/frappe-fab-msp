import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fab_msp import ticket


class Doc(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)

    def __getattr__(self, key):
        return self.get(key)

    def __setattr__(self, key, value):
        self[key] = value


def ticket_type(**flags):
    tt = Doc(flags)
    return tt


class TestApplyServiceRules(unittest.TestCase):
    def _run(self, doc, tt):
        stub = SimpleNamespace(get_cached_doc=lambda dt, name: tt)
        with patch.object(ticket, "frappe", stub):
            ticket.apply_service_rules(doc)

    def test_requires_approval_holds_at_awaiting_service(self):
        doc = Doc(ticket_type="T")
        self._run(doc, ticket_type(fab_requires_approval=1))
        self.assertEqual(doc.fab_approval_status, "Awaiting Service")

    def test_no_approval_is_not_required(self):
        doc = Doc(ticket_type="T")
        self._run(doc, ticket_type(fab_requires_approval=0))
        self.assertEqual(doc.fab_approval_status, "Not Required")

    def test_in_flight_status_not_downgraded(self):
        for state in ("Pending", "Approved", "Rejected"):
            doc = Doc(ticket_type="T", fab_approval_status=state)
            self._run(doc, ticket_type(fab_requires_approval=1))
            self.assertEqual(doc.fab_approval_status, state)

    def test_billing_status_from_type(self):
        doc = Doc(ticket_type="T")
        self._run(doc, ticket_type(fab_requires_approval=0, fab_billable=1))
        self.assertEqual(doc.fab_billing_status, "Pending")
        doc = Doc(ticket_type="T")
        self._run(doc, ticket_type(fab_requires_approval=0, fab_billable=0))
        self.assertEqual(doc.fab_billing_status, "Not Billable")

    def test_no_ticket_type_is_noop(self):
        doc = Doc()
        self._run(doc, ticket_type())
        self.assertIsNone(doc.get("fab_approval_status"))


class TestMaybeFulfillOnClose(unittest.TestCase):
    def _run(self, doc):
        called = []
        with patch("fab_msp.fulfillment.fulfill_ticket", side_effect=lambda n: called.append(n)):
            ticket.maybe_fulfill_on_close(doc)
        return called

    def test_fires_on_close_when_billable_and_no_approval_needed(self):
        doc = Doc(name="T1", status="Closed", fab_billing_status="Pending", fab_approval_status="Not Required")
        self.assertEqual(self._run(doc), ["T1"])

    def test_fires_after_approval(self):
        doc = Doc(name="T2", status="Resolved", fab_billing_status="Pending", fab_approval_status="Approved")
        self.assertEqual(self._run(doc), ["T2"])

    def test_skips_when_open(self):
        doc = Doc(name="T3", status="Open", fab_billing_status="Pending", fab_approval_status="Not Required")
        self.assertEqual(self._run(doc), [])

    def test_skips_when_awaiting_approval(self):
        doc = Doc(name="T4", status="Closed", fab_billing_status="Pending", fab_approval_status="Pending")
        self.assertEqual(self._run(doc), [])

    def test_skips_when_not_billable(self):
        doc = Doc(name="T5", status="Closed", fab_billing_status="Not Billable", fab_approval_status="Not Required")
        self.assertEqual(self._run(doc), [])


if __name__ == "__main__":
    unittest.main()
