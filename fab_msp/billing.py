from __future__ import annotations

import frappe
from frappe.utils import flt, today

from fab_msp.fulfillment import (
    _customer_rate,
    _customer_tax_template,
    _erp_customer,
    set_payment_schedule,
)


def reflect_invoice_on_submit(doc, method=None):
    """When an MSP-generated Sales Invoice is submitted, mark the originating
    tickets Invoiced. Sending to SdI stays the standard fab_italy_edi action on
    the submitted invoice."""
    tickets = set(
        frappe.get_all("HD Ticket", filters={"fab_sales_invoice": doc.name}, pluck="name")
    )
    tickets.update(
        t
        for t in frappe.get_all(
            "MSP Billing Charge", filters={"sales_invoice": doc.name}, pluck="hd_ticket"
        )
        if t
    )
    for ticket in tickets:
        frappe.db.set_value("HD Ticket", ticket, "fab_billing_status", "Invoiced")


@frappe.whitelist()
def generate_consolidated_invoices(posting_date: str | None = None) -> list[str]:
    """Create one draft consolidated invoice per consolidated customer.

    Manual month-end run. Invoices are left as draft for review before submit.
    """
    posting_date = posting_date or today()
    created = []
    for customer in frappe.get_all(
        "Customer", filters={"fab_msp_billing_mode": "Consolidated"}, pluck="name"
    ):
        name = generate_for_customer(customer, posting_date)
        if name:
            created.append(name)
    frappe.db.commit()
    return created


def build_consolidated_items(posting_date, pools, charges, rate_fn) -> list[dict]:
    """Pure line builder for a consolidated invoice.

    - monthly services bill every month; annual only in their renewal month
      (matched by month-of-year);
    - the base excludes seats added this period (they arrive as addition lines);
    - deferred additions are appended as their own lines.
    """
    added_by_pool: dict[str, float] = {}
    for c in charges:
        if c.get("customer_service"):
            added_by_pool[c["customer_service"]] = added_by_pool.get(c["customer_service"], 0) + flt(c["qty"])

    month_of_year = posting_date[5:7]
    items: list[dict] = []
    for pool in pools:
        base_qty = flt(pool["quantity"]) - added_by_pool.get(pool["name"], 0)
        if not pool.get("billing_item") or base_qty <= 0:
            continue
        rate = flt(rate_fn(pool["billing_item"]))
        if (pool.get("billing_interval") or "Monthly") == "Annual":
            if not pool.get("renewal_date") or str(pool["renewal_date"])[5:7] != month_of_year:
                continue
            description = f"{pool['service_label']} - annual {str(pool['renewal_date'])[:4]}"
        else:
            description = f"{pool['service_label']} - recurring {posting_date[:7]}"
        items.append(
            {"item_code": pool["billing_item"], "qty": base_qty, "rate": rate,
             "price_list_rate": rate, "description": description}
        )

    for c in charges:
        items.append(
            {"item_code": c["item"], "qty": c["qty"], "rate": c["rate"],
             "price_list_rate": c["rate"], "description": c.get("description")}
        )
    return items


@frappe.whitelist()
def generate_for_customer(customer: str, posting_date: str | None = None) -> str | None:
    """One draft invoice for a customer: recurring base of active pools plus the
    period's deferred additions. The base excludes the seats added this period
    (they come in as their own addition lines) so nothing is billed twice."""
    posting_date = posting_date or today()
    erp = _erp_customer(customer)

    charges = frappe.get_all(
        "MSP Billing Charge",
        filters={"customer": erp, "status": "Unbilled"},
        fields=["name", "item", "qty", "rate", "description", "customer_service"],
    )
    pools = frappe.get_all(
        "Customer Service",
        filters={"customer": customer, "status": "Active", "billing_mode": "Recurring"},
        fields=["name", "billing_item", "quantity", "service_label", "billing_interval", "renewal_date"],
    )
    items = build_consolidated_items(
        posting_date,
        [dict(p) for p in pools],
        [dict(c) for c in charges],
        lambda item: _customer_rate(customer, item, None),
    )
    if not items:
        return None

    si = frappe.get_doc(
        {
            "doctype": "Sales Invoice",
            "customer": erp,
            "posting_date": posting_date,
            "ignore_pricing_rule": 1,
            "items": items,
            "remarks": f"MSP consolidated invoice {posting_date[:7]}",
        }
    )
    template = _customer_tax_template(erp)
    if template:
        from erpnext.controllers.accounts_controller import get_taxes_and_charges

        si.taxes_and_charges = template
        for tax in get_taxes_and_charges("Sales Taxes and Charges Template", template):
            si.append("taxes", tax)
    set_payment_schedule(si)
    si.flags.ignore_permissions = True
    si.insert()  # draft; operator submits at month end

    for c in charges:
        frappe.db.set_value(
            "MSP Billing Charge", c.name, {"status": "Billed", "sales_invoice": si.name}
        )
    return si.name
