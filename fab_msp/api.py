from __future__ import annotations

import frappe
from frappe import _

APPROVER_ROLES = {"Agent Manager", "System Manager"}


@frappe.whitelist()
def create_service_from_ticket(ticket: str) -> str:
    """Provision a Customer Service from a ticket and link it back.

    The service type comes from the ticket's HD Ticket Type (fab_service_type).
    Used by the 'Create service' action in the agent ticket view.
    """
    t = frappe.get_doc("HD Ticket", ticket)
    if not t.customer:
        frappe.throw(_("This ticket has no customer set."))
    if t.get("fab_customer_service"):
        frappe.throw(_("This ticket is already linked to a service."))

    service_type = frappe.db.get_value("HD Ticket Type", t.ticket_type, "fab_service_type") if t.ticket_type else None
    if not service_type:
        frappe.throw(_("The ticket type has no Service Type configured; set it on the ticket type first."))

    cs = frappe.get_doc(
        {
            "doctype": "Customer Service",
            "customer": t.customer,
            "service_type": service_type,
            "service_label": t.subject or f"Service for {t.name}",
        }
    )
    cs.insert()
    t.db_set("fab_customer_service", cs.name)
    return cs.name


@frappe.whitelist()
def set_ticket_approval(ticket: str, decision: str) -> str:
    """Approve or reject a ticket's service request."""
    if decision not in ("Approved", "Rejected"):
        frappe.throw(_("Invalid decision."))
    if not (APPROVER_ROLES & set(frappe.get_roles())):
        frappe.throw(_("You are not permitted to approve requests."))
    if frappe.db.get_value("HD Ticket", ticket, "fab_approval_status") != "Pending":
        frappe.throw(_("This request is not pending approval."))
    frappe.db.set_value("HD Ticket", ticket, "fab_approval_status", decision)
    return decision
