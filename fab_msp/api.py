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


def _ticket_type_rules(ticket_doc) -> tuple[str, str]:
    approval_by, cmdb_effect = "Internal Manager", "None"
    if ticket_doc.ticket_type:
        vals = frappe.db.get_value(
            "HD Ticket Type", ticket_doc.ticket_type, ["fab_approval_by", "fab_cmdb_effect"]
        )
        if vals:
            approval_by, cmdb_effect = vals[0] or approval_by, vals[1] or cmdb_effect
    return approval_by, cmdb_effect


def _can_approve(ticket_doc, user: str) -> bool:
    approval_by, _ = _ticket_type_rules(ticket_doc)
    if approval_by == "Customer Manager":
        return bool(ticket_doc.customer) and _is_customer_manager(ticket_doc.customer, user)
    return bool(INTERNAL_APPROVER_ROLES & set(frappe.get_roles(user)))


@frappe.whitelist()
def can_approve_ticket(ticket: str) -> bool:
    """Whether the current user may approve this ticket right now.

    Drives button visibility in the ticket views so the action only shows to the
    actual approver (the customer's manager, or our staff, per the ticket type).
    """
    t = frappe.get_doc("HD Ticket", ticket)
    if t.get("fab_approval_status") != "Pending":
        return False
    return _can_approve(t, frappe.session.user)


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

    _, cmdb_effect = _ticket_type_rules(t)
    if cmdb_effect == "Modify" and not t.get("fab_customer_service"):
        frappe.throw(_("Link a service before approving this request."))

    if not _can_approve(t, frappe.session.user):
        frappe.throw(_("You are not permitted to approve this request."))

    t.db_set("fab_approval_status", decision)
    if decision == "Approved":
        from fab_msp.fulfillment import fulfill_ticket

        fulfill_ticket(t.name)
    return decision
