from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

# MSP fields surfaced to agents inside the Helpdesk SPA ticket view. The SPA
# renders whatever the Default ticket template lists, so registering them there
# is how we extend the agent view without touching the Vue frontend.
AGENT_TEMPLATE_FIELDS = [
    "fab_customer_service",
    "fab_approval_status",
    "fab_billing_status",
    "fab_sales_invoice",
    "fab_subscription",
]


def after_install():
    ensure_custom_fields()
    ensure_ticket_template_fields()


def after_migrate():
    ensure_custom_fields()
    ensure_ticket_template_fields()


def ensure_custom_fields():
    create_custom_fields(get_custom_fields(), ignore_validate=True)


def ensure_ticket_template_fields():
    """Expose the MSP ticket fields in the agent SPA via the Default template.

    Idempotent: only appends fields that are not already on the template. All
    are hidden from the customer portal (internal fulfillment data).
    """
    if not frappe.db.exists("HD Ticket Template", "Default"):
        return
    template = frappe.get_doc("HD Ticket Template", "Default")
    present = {row.fieldname for row in template.fields}
    changed = False
    for fieldname in AGENT_TEMPLATE_FIELDS:
        if fieldname not in present:
            template.append("fields", {"fieldname": fieldname, "hide_from_customer": 1})
            changed = True
    if changed:
        template.flags.ignore_permissions = True
        template.save()


def get_custom_fields() -> dict:
    return {
        # Service catalog: rules attached to a request category
        "HD Ticket Type": [
            {
                "fieldname": "fab_service_section",
                "label": "Service Request Rules",
                "fieldtype": "Section Break",
                "insert_after": "priority",
                "collapsible": 1,
            },
            {
                "fieldname": "fab_requires_approval",
                "label": "Requires Approval",
                "fieldtype": "Check",
                "insert_after": "fab_service_section",
            },
            {
                "fieldname": "fab_approval_by",
                "label": "Approval By",
                "fieldtype": "Select",
                "options": "Internal Manager\nCustomer Manager",
                "default": "Internal Manager",
                "depends_on": "eval:doc.fab_requires_approval",
                "insert_after": "fab_requires_approval",
            },
            {
                "fieldname": "fab_cmdb_effect",
                "label": "CMDB Effect",
                "fieldtype": "Select",
                "options": "None\nCreate\nModify",
                "default": "None",
                "insert_after": "fab_approval_by",
                "description": "Create: the request provisions a new Customer Service. Modify: it acts on an existing one.",
            },
            {
                "fieldname": "fab_service_type",
                "label": "Service Type",
                "fieldtype": "Link",
                "options": "Customer Service Type",
                "depends_on": "eval:doc.fab_cmdb_effect=='Create'",
                "insert_after": "fab_cmdb_effect",
            },
            {
                "fieldname": "fab_service_col",
                "fieldtype": "Column Break",
                "insert_after": "fab_service_type",
            },
            {
                "fieldname": "fab_billable",
                "label": "Billable",
                "fieldtype": "Check",
                "insert_after": "fab_service_col",
            },
            {
                "fieldname": "fab_billing_item",
                "label": "Billing Item",
                "fieldtype": "Link",
                "options": "Item",
                "depends_on": "eval:doc.fab_billable",
                "insert_after": "fab_billable",
            },
            {
                "fieldname": "fab_billing_mode",
                "label": "Billing Mode",
                "fieldtype": "Select",
                "options": "One-time\nRecurring",
                "default": "One-time",
                "depends_on": "eval:doc.fab_billable",
                "insert_after": "fab_billing_item",
            },
            {
                "fieldname": "fab_coterm_prorate",
                "label": "Co-terminate & pro-rate",
                "fieldtype": "Check",
                "depends_on": "eval:doc.fab_billable",
                "insert_after": "fab_billing_mode",
                "description": "Align the added quantity to the existing service term and bill the delta pro-rata.",
            },
        ],
        # The request instance links to the customer's service and carries state
        "HD Ticket": [
            {
                "fieldname": "fab_service_section",
                "label": "Managed Service",
                "fieldtype": "Section Break",
                "insert_after": "ticket_type",
                "collapsible": 1,
            },
            {
                "fieldname": "fab_customer_service",
                "label": "Customer Service",
                "fieldtype": "Link",
                "options": "Customer Service",
                "insert_after": "fab_service_section",
                "description": "The service this request acts on (for modify/link requests).",
            },
            {
                "fieldname": "fab_approval_status",
                "label": "Approval Status",
                "fieldtype": "Select",
                "options": "Not Required\nPending\nApproved\nRejected",
                "default": "Not Required",
                "read_only": 1,
                "insert_after": "fab_customer_service",
            },
            {
                "fieldname": "fab_service_col",
                "fieldtype": "Column Break",
                "insert_after": "fab_approval_status",
            },
            {
                "fieldname": "fab_billing_status",
                "label": "Billing Status",
                "fieldtype": "Select",
                "options": "Not Billable\nPending\nInvoiced\nSubscribed",
                "default": "Not Billable",
                "read_only": 1,
                "insert_after": "fab_service_col",
            },
            {
                "fieldname": "fab_sales_invoice",
                "label": "Sales Invoice",
                "fieldtype": "Link",
                "options": "Sales Invoice",
                "read_only": 1,
                "insert_after": "fab_billing_status",
            },
            {
                "fieldname": "fab_subscription",
                "label": "Subscription",
                "fieldtype": "Link",
                "options": "Subscription",
                "read_only": 1,
                "insert_after": "fab_sales_invoice",
            },
        ],
    }
