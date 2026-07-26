from __future__ import annotations

import frappe
from frappe import _
from helpdesk.helpdesk.doctype.hd_ticket.hd_ticket import _is_customer_manager
from helpdesk.utils import get_helpdesk_url

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
def request_approval(ticket: str) -> str:
    """Send a provisioned request to its approver.

    Only valid once a service is linked and the request is Awaiting Service. Sets
    Pending and notifies the approver(s), so a manager is asked only for a real,
    completed service.
    """
    t = frappe.get_doc("HD Ticket", ticket)
    if not t.get("fab_customer_service"):
        frappe.throw(_("Provision or link a service before requesting approval."))
    if t.get("fab_approval_status") != "Awaiting Service":
        frappe.throw(_("This request is not awaiting approval."))
    approval_by, _effect = _ticket_type_rules(t)
    t.db_set("fab_approval_status", "Pending")
    _notify_approvers(t, approval_by)
    return "Pending"


def _approver_emails(ticket_doc, approval_by) -> list[str]:
    if approval_by == "Customer Manager":
        if not ticket_doc.customer or not frappe.db.exists("HD Customer", ticket_doc.customer):
            return []
        customer = frappe.get_doc("HD Customer", ticket_doc.customer)
        emails = []
        for member in customer.get("contacts", []):
            if member.get("is_manager") and member.contact_name:
                email = frappe.db.get_value("Contact", member.contact_name, "email_id")
                if email:
                    emails.append(email)
        return list(dict.fromkeys(emails))
    users = set()
    for role in INTERNAL_APPROVER_ROLES:
        users.update(
            frappe.get_all("Has Role", filters={"role": role, "parenttype": "User"}, pluck="parent")
        )
    return [u for u in users if "@" in u and frappe.db.get_value("User", u, "enabled")]


def _notify_approvers(ticket_doc, approval_by) -> None:
    recipients = _approver_emails(ticket_doc, approval_by)
    if not recipients:
        return
    path = "/helpdesk/my-tickets/" if approval_by == "Customer Manager" else "/helpdesk/tickets/"
    context = {
        "subject": ticket_doc.subject or ticket_doc.name,
        "ticket": ticket_doc.name,
        "link": get_helpdesk_url(path + ticket_doc.name),
    }
    template = (
        frappe.get_doc("Email Template", "MSP Approval Requested")
        if frappe.db.exists("Email Template", "MSP Approval Requested")
        else None
    )
    default_lang = frappe.db.get_single_value("System Settings", "language") or "en"
    original_lang = frappe.local.lang

    # render per recipient so each gets the mail in their own language
    for recipient in recipients:
        frappe.local.lang = frappe.db.get_value("User", recipient, "language") or default_lang
        try:
            if template:
                subject = frappe.render_template(template.subject, context)
                message = frappe.render_template(template.response_html or template.response, context)
            else:
                subject = _("Approval requested") + ": " + context["subject"]
                message = (
                    "<p>" + _("A service request needs your approval.") + "</p>"
                    + f'<p><a href="{context["link"]}">{ticket_doc.name}</a></p>'
                )
            frappe.sendmail(
                recipients=[recipient],
                subject=subject,
                message=message,
                reference_doctype="HD Ticket",
                reference_name=ticket_doc.name,
            )
        finally:
            frappe.local.lang = original_lang


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

    _approval_by, cmdb_effect = _ticket_type_rules(t)
    if cmdb_effect == "Modify" and not t.get("fab_customer_service"):
        frappe.throw(_("Link a service before approving this request."))

    if not _can_approve(t, frappe.session.user):
        frappe.throw(_("You are not permitted to approve this request."))

    t.db_set("fab_approval_status", decision)
    if decision == "Approved":
        from fab_msp.fulfillment import fulfill_ticket

        fulfill_ticket(t.name)
    return decision
