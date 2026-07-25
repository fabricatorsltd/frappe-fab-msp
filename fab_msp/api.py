from __future__ import annotations

import frappe
from frappe import _
from helpdesk.helpdesk.doctype.hd_ticket.hd_ticket import _is_customer_manager

INTERNAL_APPROVER_ROLES = {"Agent Manager", "System Manager"}


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
    """Approve or reject a ticket's service request.

    Routing follows the ticket type's Approval By: 'Customer Manager' requests
    can only be decided by a manager of the ticket's customer (the requester's
    business responsible, not our staff); 'Internal Manager' requests by our
    Agent/System managers. Modify requests need a linked service first.
    """
    if decision not in ("Approved", "Rejected"):
        frappe.throw(_("Invalid decision."))
    t = frappe.get_doc("HD Ticket", ticket)
    if t.get("fab_approval_status") != "Pending":
        frappe.throw(_("This request is not pending approval."))

    approval_by = "Internal Manager"
    cmdb_effect = "None"
    if t.ticket_type:
        approval_by, cmdb_effect = frappe.db.get_value(
            "HD Ticket Type", t.ticket_type, ["fab_approval_by", "fab_cmdb_effect"]
        ) or (approval_by, cmdb_effect)

    if cmdb_effect == "Modify" and not t.get("fab_customer_service"):
        frappe.throw(_("Link a service before approving this request."))

    user = frappe.session.user
    if approval_by == "Customer Manager":
        allowed = bool(t.customer) and _is_customer_manager(t.customer, user)
    else:
        allowed = bool(INTERNAL_APPROVER_ROLES & set(frappe.get_roles(user)))
    if not allowed:
        frappe.throw(_("You are not permitted to approve this request."))

    t.db_set("fab_approval_status", decision)
    return decision
