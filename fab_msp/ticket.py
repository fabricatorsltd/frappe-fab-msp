from __future__ import annotations

import frappe


IN_FLIGHT_APPROVAL = ("Pending", "Approved", "Rejected")


def apply_service_rules(doc, method=None):
    """Derive approval and billing state on a ticket from its ticket type.

    Approval is never auto-set to Pending: a request only reaches a manager once
    an agent has provisioned the service and explicitly asked for approval
    (fab_msp.api.request_approval). Until then a request that needs approval sits
    at "Awaiting Service". In-flight states are never downgraded here.
    """
    if not doc.get("ticket_type"):
        return

    ticket_type = frappe.get_cached_doc("HD Ticket Type", doc.ticket_type)

    if doc.get("fab_approval_status") not in IN_FLIGHT_APPROVAL:
        doc.fab_approval_status = (
            "Awaiting Service" if ticket_type.get("fab_requires_approval") else "Not Required"
        )

    if doc.get("fab_billing_status") in (None, "", "Not Billable"):
        doc.fab_billing_status = "Pending" if ticket_type.get("fab_billable") else "Not Billable"


def maybe_fulfill_on_close(doc, method=None):
    """Fulfil a billable request that needs no approval when the ticket closes.

    Requests that require approval are fulfilled on approval instead. Fulfillment
    is idempotent, so a request already billed on approval is a no-op here.
    """
    if doc.get("status") not in ("Resolved", "Closed"):
        return
    if doc.get("fab_billing_status") != "Pending":
        return
    if doc.get("fab_approval_status") not in ("Not Required", "Approved"):
        return
    from fab_msp.fulfillment import fulfill_ticket

    fulfill_ticket(doc.name)
