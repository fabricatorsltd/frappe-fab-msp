from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import escape_html, fmt_money, format_date, format_datetime, now_datetime
from helpdesk.helpdesk.doctype.hd_settings.helpers import (
    DETAIL_LABEL,
    DETAIL_VALUE,
    branded_email,
    customer_portal,
    get_default_language,
    mail_styles,
    resolve_ticket_language,
    use_language,
)
from helpdesk.helpdesk.doctype.hd_ticket.hd_ticket import _is_customer_manager
from helpdesk.helpdesk.utils.email import helpdesk_outgoing_email_account
from helpdesk.utils import get_helpdesk_url

APPROVAL_STATUS = "Awaiting Approval"

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


@frappe.whitelist(methods=["POST"])
def setup_field_technician(user: str, supplier: str) -> str:
    """Give an external technician desk access to their own interventions.

    The Field Technician role opens the Task form; the User Permission on the
    supplier is what keeps them to their own jobs. Idempotent. It hands out a
    role and lifts a portal user to System User, so only a System Manager runs it.
    """
    frappe.only_for("System Manager")
    from fab_msp.field_service import FIELD_TECHNICIAN_ROLE

    doc = frappe.get_doc("User", user)
    if doc.user_type != "System User":
        doc.user_type = "System User"
    if FIELD_TECHNICIAN_ROLE not in {r.role for r in doc.get("roles", [])}:
        doc.append("roles", {"role": FIELD_TECHNICIAN_ROLE})
    doc.save(ignore_permissions=True)

    if not frappe.db.exists(
        "User Permission", {"user": user, "allow": "Supplier", "for_value": supplier}
    ):
        permission = frappe.get_doc(
            {
                "doctype": "User Permission",
                "user": user,
                "allow": "Supplier",
                "for_value": supplier,
                "apply_to_all_doctypes": 1,
            }
        )
        permission.insert(ignore_permissions=True)
    return user


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
    # lock the row so a double click cannot send the request twice
    t = frappe.get_doc("HD Ticket", ticket, for_update=True)
    if not t.get("fab_customer_service"):
        frappe.throw(_("Provision or link a service before requesting approval."))
    if t.get("fab_approval_status") != "Awaiting Service":
        frappe.throw(_("This request is not awaiting approval."))
    approval_by, _effect = _ticket_type_rules(t)
    t.db_set("fab_approval_status", "Pending")
    # parked on a status of its own: the inactivity reminder and auto-close watch
    # Replied and must not close a request the manager has not decided yet
    if frappe.db.exists("HD Ticket Status", APPROVAL_STATUS):
        _set_status(t, APPROVAL_STATUS)
    else:
        _move_to_category(t, "Paused")
    _notify_approvers(t, approval_by)
    return "Pending"


def _status_for_category(statuses, category) -> str | None:
    """The first status of `category` by its order, from HD Ticket Status rows."""
    rows = [s for s in statuses if s.get("category") == category]
    rows.sort(key=lambda s: (s.get("order") is None, s.get("order") or 0, s.get("name")))
    return rows[0]["name"] if rows else None


def _set_status(ticket_doc, status) -> None:
    if not status or ticket_doc.status == status:
        return
    ticket_doc.status = status
    ticket_doc.status_category = frappe.db.get_value("HD Ticket Status", status, "category")
    ticket_doc.flags.ignore_permissions = True
    ticket_doc.save()


def _move_to_category(ticket_doc, category) -> None:
    if ticket_doc.get("status_category") == category:
        return
    statuses = frappe.get_all(
        "HD Ticket Status", filters={"enabled": 1}, fields=["name", "category", "order"]
    )
    status = _status_for_category(statuses, category)
    if not status or ticket_doc.status == status:
        return
    ticket_doc.status = status
    ticket_doc.status_category = category
    ticket_doc.flags.ignore_permissions = True
    ticket_doc.save()


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
    link = get_helpdesk_url(path + ticket_doc.name)
    from fab_msp.fulfillment import quote_ticket

    quote = quote_ticket(ticket_doc)
    requester = _requester_name(ticket_doc)
    default_lang = frappe.db.get_single_value("System Settings", "language") or "en"

    # one mail per language, so approvers who share a language share one thread entry
    by_lang: dict[str, list[str]] = {}
    for recipient in recipients:
        lang = frappe.db.get_value("User", recipient, "language") or default_lang
        by_lang.setdefault(lang, []).append(recipient)
    for lang, group in by_lang.items():
        with use_language(lang):
            subject = _("Approval needed: {0} (#{1})").format(ticket_doc.subject or ticket_doc.name, ticket_doc.name)
            body = _approval_request_body(quote, requester, link)
            _send_ticket_mail(ticket_doc, group, subject, body)


def _requester_name(ticket_doc) -> str:
    name = ticket_doc.get("contact") and frappe.db.get_value("Contact", ticket_doc.contact, "full_name")
    raised_by = ticket_doc.get("raised_by") or ""
    return name or frappe.db.get_value("User", raised_by, "full_name") or raised_by


def _money(amount, currency) -> str:
    return f"{currency} {fmt_money(amount, 2)}" if currency else fmt_money(amount, 2)


def _change_text(quote) -> str:
    qty, current = quote["qty"], quote.get("current_qty")
    if current is None:
        return (_("+{0} licence") if qty == 1 else _("+{0} licences")).format(qty)
    text = _("+{0} licence ({1} -> {2})") if qty == 1 else _("+{0} licences ({1} -> {2})")
    return text.format(qty, current, current + qty)


def _cost_lines(quote, money, date) -> list[str]:
    """The cost of the change as fulfillment will bill it, one line each; none
    when there is no price to show. `money` and `date` format the values."""
    if quote.get("kind") == "prorata":
        per = (
            _("({0} per licence per month after that)")
            if quote.get("interval") == "Monthly"
            else _("({0} per licence per year after that)")
        )
        return [
            _("{0} now, pro-rata until the {1} renewal").format(money(quote["total"]), date(quote["renewal_date"])),
            per.format(money(quote["rate"])),
        ]
    if quote.get("kind") == "one_time":
        return [_("{0}, one-time charge").format(money(quote["total"]))]
    return []


def _detail_table(rows) -> str:
    cells = "\n".join(
        f'  <tr><td style="{DETAIL_LABEL}">{label}</td><td style="{DETAIL_VALUE}">{value}</td></tr>'
        for label, value in rows
    )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="margin:0 0 20px;border-collapse:collapse;font-size:14px;">\n{cells}\n</table>'
    )


def _approval_request_body(quote, requester, link) -> str:
    style = mail_styles()
    rows = []
    if quote.get("service"):
        rows.append((_("Service", context="Approval mail"), escape_html(quote["service"])))
    rows.append((_("Change", context="Approval mail"), escape_html(_change_text(quote))))
    currency = quote.get("currency")
    cost = _cost_lines(
        quote, lambda amount: _money(amount, currency), lambda d: format_date(d, "dd/mm/yyyy")
    )
    if cost:
        rows.append((_("Cost", context="Approval mail"), "<br>".join(escape_html(line) for line in cost)))
    return f"""\
<p>{_("Hello,")}</p>
<p>{escape_html(_("{0} has asked us for a change that needs a manager's approval:").format(requester))}</p>
{_detail_table(rows)}
<p>{_("Please open the request and click Approve or Reject:")}</p>
<p><a href="{link}" style="{style.button}">{_("Open the request")}</a></p>
<p>{_("We will not make the change until it is approved.")}</p>
"""


def _decision_body(ticket_doc, decision, approver) -> str:
    style = mail_styles()
    if decision == "Approved":
        text = _("Your request #{0} was approved by {1}. We will now proceed with the change.")
    else:
        text = _("Your request #{0} was rejected by {1}, so we will not make the change.")
    link = ""
    if customer_portal():
        link = f'\n<p><a href="{ticket_doc.portal_uri}" style="{style.link}">{_("Open the request")}</a></p>'
    return f"""\
<p>{_("Hello,")}</p>
<p>{escape_html(text.format(ticket_doc.name, approver))}</p>
<p style="{style.request_box}"><strong>{_("Request no.")} {ticket_doc.name}</strong><br>{escape_html(ticket_doc.subject or "")}</p>{link}
"""


def _send_ticket_mail(ticket_doc, recipients, subject, body, marker=False) -> None:
    """Mail `body` from the helpdesk mailbox in the site's branded page and
    file it on the ticket thread. Call it in the recipient's language.

    Filed as an automated message, like the inactivity mails, so it is not
    taken for an agent reply (first response, SLA, auto status)."""
    sender = helpdesk_outgoing_email_account()
    if not sender:
        frappe.throw(_("No outgoing Email Account is configured for the helpdesk"))
    email = branded_email(body, subject, marker=marker)
    last = ticket_doc._last_threadable_communication()
    communication = frappe.get_doc(
        {
            "doctype": "Communication",
            "communication_medium": "Email",
            "communication_type": "Automated Message",
            "content": body,
            "email_account": sender.name,
            "email_status": "Open",
            "recipients": ", ".join(recipients),
            "reference_doctype": "HD Ticket",
            "reference_name": ticket_doc.name,
            "sender": sender.email_id,
            "sent_or_received": "Sent",
            "status": "Linked",
            "subject": subject,
        }
    )
    if last:
        communication.in_reply_to = last
    communication.insert(ignore_permissions=True)
    frappe.sendmail(
        communication=communication.name,
        recipients=recipients,
        reference_doctype="HD Ticket",
        reference_name=ticket_doc.name,
        reply_to=sender.email_id,
        sender=sender.email_id,
        subject=subject,
        with_container=False,
        in_reply_to=last,
        email_headers={"X-Auto-Generated": "msp-approval"},
        **email,
    )


def _record_decision(ticket_doc, decision) -> None:
    """Note the decision on the ticket for the agents, reopen it and tell the
    requester."""
    user = frappe.session.user
    approver = frappe.db.get_value("User", user, "full_name") or user
    with use_language(get_default_language()):
        text = _("Approved by {0} on {1}.") if decision == "Approved" else _("Rejected by {0} on {1}.")
        content = escape_html(text.format(approver, format_datetime(now_datetime())))
    comment = frappe.new_doc("HD Ticket Comment")
    comment.commented_by = user
    comment.content = f"<p>{content}</p>"
    comment.reference_ticket = ticket_doc.name
    comment.insert(ignore_permissions=True)

    # reopen only a ticket still parked for the decision, never one an agent resolved
    if ticket_doc.get("status_category") == "Paused":
        reopen = frappe.db.get_single_value("HD Settings", "ticket_reopen_status")
        if reopen:
            _set_status(ticket_doc, reopen)
        else:
            _move_to_category(ticket_doc, "Open")

    addresses = ticket_doc._strip_own_mailboxes(ticket_doc.raised_by) if ticket_doc.raised_by else []
    if not addresses:
        return
    with use_language(resolve_ticket_language(ticket_doc)):
        subject_text = _("Request approved: {0} (#{1})") if decision == "Approved" else _("Request rejected: {0} (#{1})")
        subject = subject_text.format(ticket_doc.subject or ticket_doc.name, ticket_doc.name)
        body = _decision_body(ticket_doc, decision, approver)
        _send_ticket_mail(ticket_doc, addresses[:1], subject, body, marker=True)


def _notify_agents_of_decision(ticket_doc, decision, approver) -> None:
    """Tell the agents the request was decided, like the new reply mail:
    the assigned agents, or the whole active team on an unassigned ticket."""
    active = frappe.get_all("HD Agent", filters={"is_active": 1}, pluck="name")
    assigned = {a.get("name") for a in (ticket_doc.get_assigned_agents() or [])}
    user = frappe.session.user.lower()
    recipients = [a for a in ([a for a in active if a in assigned] or active) if "@" in a and a.lower() != user]
    if not recipients:
        return
    try:
        with use_language(get_default_language()):
            subject_text = _("[Approved] #{0}: {1}") if decision == "Approved" else _("[Rejected] #{0}: {1}")
            subject = subject_text.format(ticket_doc.name, ticket_doc.subject or "")
            frappe.sendmail(
                recipients=recipients,
                subject=subject,
                **branded_email(_decision_agent_body(ticket_doc, decision, approver), subject),
                reference_doctype="HD Ticket",
                reference_name=ticket_doc.name,
                email_headers={"X-Auto-Generated": "msp-decision-agents"},
            )
    except Exception:
        ticket_doc.log_error("Could not notify agents of the approval decision")


def _decision_agent_body(ticket_doc, decision, approver) -> str:
    style = mail_styles()
    if decision == "Approved":
        intro = _("{0} approved the request. Go ahead with the change.")
    else:
        intro = _("{0} rejected the request. Do not make the change.")
    rows = [(_("Customer"), escape_html(ticket_doc.customer or "-"))]
    if ticket_doc.get("fab_customer_service"):
        rows.append((_("Service", context="Approval mail"), escape_html(ticket_doc.fab_customer_service)))
    if ticket_doc.get("fab_quantity"):
        rows.append((_("Quantity"), escape_html(str(ticket_doc.fab_quantity))))
    link = frappe.utils.get_url("/helpdesk/tickets/" + str(ticket_doc.name))
    return f"""\
<p style="margin:0 0 8px;">{escape_html(intro.format(approver))}</p>
<p style="margin:0 0 20px;font-size:18px;font-weight:600;line-height:1.4;">#{ticket_doc.name} {escape_html(ticket_doc.subject or "")}</p>
{_detail_table(rows)}
<p style="margin:0;"><a href="{link}" style="{style.button}">{_("Open the ticket")}</a></p>
"""


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
    # lock the row so a double click cannot record the decision twice
    t = frappe.get_doc("HD Ticket", ticket, for_update=True)
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
        # fulfillment writes the ticket through its own copy
        t = frappe.get_doc("HD Ticket", ticket)
    _record_decision(t, decision)
    _notify_agents_of_decision(t, decision, frappe.db.get_value("User", frappe.session.user, "full_name") or frappe.session.user)
    return decision
