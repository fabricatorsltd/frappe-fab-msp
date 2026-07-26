from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

# MSP fields surfaced to agents inside the Helpdesk SPA ticket view. The SPA
# renders whatever the Default ticket template lists, so registering them there
# is how we extend the agent view without touching the Vue frontend.
AGENT_TEMPLATE_FIELDS = [
    "fab_customer_service",
    "fab_quantity",
    "fab_approval_status",
    "fab_billing_status",
    "fab_sales_invoice",
    "fab_subscription",
]


FORM_SCRIPT_NAME = "MSP Ticket Actions"

FORM_SCRIPT = """
async function setupForm({ doc, call, updateField, createToast }) {
  const t = window.__ || ((s) => s);
  const ok = (title) => createToast({ title, icon: "check", iconClasses: "text-green-600" });
  const err = (title) => createToast({ title, icon: "x", iconClasses: "text-red-600" });
  const actions = [];

  const openDoc = (dt, name) =>
    window.open(window.location.origin + "/app/" + dt + "/" + encodeURIComponent(name), "_blank");

  if (!doc.fab_customer_service) {
    actions.push({
      label: t("Create service"),
      iconLeft: "plus-circle",
      onClick: async () => {
        try {
          const name = await call("fab_msp.api.create_service_from_ticket", { ticket: doc.name });
          updateField("fab_customer_service", name);
          ok(t("Service created") + " - " + t("complete its details"));
          openDoc("customer-service", name);
        } catch (e) {
          err(e.message || t("Could not create service"));
        }
      },
    });
  } else {
    actions.push({
      label: t("Open service"),
      iconLeft: "external-link",
      onClick: () => openDoc("customer-service", doc.fab_customer_service),
    });
  }

  if (doc.fab_customer_service && doc.fab_approval_status === "Awaiting Service") {
    actions.push({
      label: t("Request approval"),
      iconLeft: "send",
      onClick: async () => {
        try {
          await call("fab_msp.api.request_approval", { ticket: doc.name });
          updateField("fab_approval_status", "Pending");
          ok(t("Approval requested"));
        } catch (e) {
          err(e.message || t("Could not request approval"));
        }
      },
    });
  }

  if (doc.fab_approval_status === "Pending" && (await call("fab_msp.api.can_approve_ticket", { ticket: doc.name }))) {
    actions.push({
      label: t("Approve"),
      iconLeft: "check",
      onClick: async () => {
        try {
          await call("fab_msp.api.set_ticket_approval", { ticket: doc.name, decision: "Approved" });
          updateField("fab_approval_status", "Approved");
          ok(t("Request approved"));
        } catch (e) {
          err(e.message || t("Could not approve"));
        }
      },
    });
    actions.push({
      label: t("Reject"),
      iconLeft: "x",
      onClick: async () => {
        try {
          await call("fab_msp.api.set_ticket_approval", { ticket: doc.name, decision: "Rejected" });
          updateField("fab_approval_status", "Rejected");
          ok(t("Request rejected"));
        } catch (e) {
          err(e.message || t("Could not reject"));
        }
      },
    });
  }

  return { actions };
}
"""


EMAIL_TEMPLATE_NAME = "MSP Approval Requested"

# Editable by operators afterwards. Jinja with {{ _(...) }} so it localises to
# the recipient's language; context: subject, link, ticket.
EMAIL_TEMPLATE_SUBJECT = '{{ _("Approval requested") }}: {{ subject }}'
EMAIL_TEMPLATE_BODY = (
    '<p>{{ _("A service request needs your approval.") }}</p>'
    '<p><a href="{{ link }}">{{ ticket }}</a></p>'
)


def after_install():
    ensure_custom_fields()
    ensure_ticket_template_fields()
    ensure_form_script()
    ensure_email_template()


def after_migrate():
    ensure_custom_fields()
    ensure_ticket_template_fields()
    ensure_form_script()
    ensure_email_template()


def ensure_email_template():
    """Seed the approval-request email as an editable Email Template.

    Created once; not overwritten on migrate so operator edits survive."""
    if not frappe.db.exists("DocType", "Email Template"):
        return
    if frappe.db.exists("Email Template", EMAIL_TEMPLATE_NAME):
        return
    doc = frappe.new_doc("Email Template")
    doc.name = EMAIL_TEMPLATE_NAME
    doc.subject = EMAIL_TEMPLATE_SUBJECT
    doc.use_html = 1
    doc.response_html = EMAIL_TEMPLATE_BODY
    doc.response = EMAIL_TEMPLATE_BODY
    doc.flags.ignore_permissions = True
    doc.insert()


FORM_SCRIPT_PORTAL_NAME = "MSP Ticket Approval (Portal)"

# Customer portal: only approve/reject, for the customer's managers. Create
# service stays agent-only. Server-side routing enforces who may decide.
FORM_SCRIPT_PORTAL = """
async function setupForm({ doc, call, updateField, createToast }) {
  const t = window.__ || ((s) => s);
  const ok = (title) => createToast({ title, icon: "check", iconClasses: "text-green-600" });
  const err = (title) => createToast({ title, icon: "x", iconClasses: "text-red-600" });
  const actions = [];

  if (doc.fab_approval_status === "Pending" && (await call("fab_msp.api.can_approve_ticket", { ticket: doc.name }))) {
    actions.push({
      label: t("Approve"),
      iconLeft: "check",
      onClick: async () => {
        try {
          await call("fab_msp.api.set_ticket_approval", { ticket: doc.name, decision: "Approved" });
          updateField("fab_approval_status", "Approved");
          ok(t("Request approved"));
        } catch (e) {
          err(e.message || t("Could not approve"));
        }
      },
    });
    actions.push({
      label: t("Reject"),
      iconLeft: "x",
      onClick: async () => {
        try {
          await call("fab_msp.api.set_ticket_approval", { ticket: doc.name, decision: "Rejected" });
          updateField("fab_approval_status", "Rejected");
          ok(t("Request rejected"));
        } catch (e) {
          err(e.message || t("Could not reject"));
        }
      },
    });
  }

  return { actions };
}
"""


def ensure_form_script():
    """Ship the ticket action scripts as HD Form Scripts. Idempotent.

    Agent view: create service + approve/reject. Customer portal: approve/reject
    only (for the customer's managers).
    """
    if not frappe.db.exists("DocType", "HD Form Script"):
        return
    _upsert_form_script(FORM_SCRIPT_NAME, FORM_SCRIPT, portal=0)
    _upsert_form_script(FORM_SCRIPT_PORTAL_NAME, FORM_SCRIPT_PORTAL, portal=1)


def _upsert_form_script(name: str, script: str, portal: int):
    if frappe.db.exists("HD Form Script", name):
        doc = frappe.get_doc("HD Form Script", name)
    else:
        doc = frappe.new_doc("HD Form Script")
        doc.name = name
    doc.update(
        {
            "dt": "HD Ticket",
            "apply_to": "Form",
            "enabled": 1,
            "apply_to_customer_portal": portal,
            "script": script.strip(),
        }
    )
    doc.flags.ignore_permissions = True
    doc.save()


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
                "fieldname": "fab_billing_interval",
                "label": "Billing Interval",
                "fieldtype": "Select",
                "options": "Monthly\nAnnual",
                "default": "Monthly",
                "depends_on": "eval:doc.fab_billing_mode=='Recurring'",
                "insert_after": "fab_billing_mode",
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
                "fieldname": "fab_quantity",
                "label": "Quantity",
                "fieldtype": "Int",
                "default": "1",
                "insert_after": "fab_customer_service",
                "description": "Seats/units this request adds; drives billing quantity.",
            },
            {
                "fieldname": "fab_approval_status",
                "label": "Approval Status",
                "fieldtype": "Select",
                "options": "Not Required\nAwaiting Service\nPending\nApproved\nRejected",
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
                "options": "Not Billable\nPending\nDeferred\nInvoiced\nSubscribed",
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
        # How the customer is billed for managed services
        "Customer": [
            {
                "fieldname": "fab_msp_billing_mode",
                "label": "MSP Billing Mode",
                "fieldtype": "Select",
                "options": "Immediate\nConsolidated",
                "default": "Immediate",
                "insert_after": "tax_category",
                "description": "Immediate: each approved request is invoiced on its own. "
                "Consolidated: additions defer to a monthly consolidated invoice.",
            },
        ],
    }
