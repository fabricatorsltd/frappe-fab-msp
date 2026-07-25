from __future__ import annotations

import frappe


def apply_service_rules(doc, method=None):
    """Derive approval and billing state on a ticket from its ticket type.

    Runs on validate. Only initialises states that are still at their default,
    so a manager's Approved/Rejected or an already-issued invoice is never
    overwritten.
    """
    if not doc.get("ticket_type"):
        return

    ticket_type = frappe.get_cached_doc("HD Ticket Type", doc.ticket_type)

    if doc.get("fab_approval_status") in (None, "", "Not Required"):
        doc.fab_approval_status = "Pending" if ticket_type.get("fab_requires_approval") else "Not Required"

    if doc.get("fab_billing_status") in (None, "", "Not Billable"):
        doc.fab_billing_status = "Pending" if ticket_type.get("fab_billable") else "Not Billable"
