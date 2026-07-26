import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fab_msp import api


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


if __name__ == "__main__":
    unittest.main()
