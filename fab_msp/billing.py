from __future__ import annotations

import frappe
from frappe.utils import flt, today

from fab_msp.fulfillment import _customer_rate, _customer_tax_template, _erp_customer


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
    added_by_pool: dict[str, float] = {}
    for c in charges:
        if c.customer_service:
            added_by_pool[c.customer_service] = added_by_pool.get(c.customer_service, 0) + flt(c.qty)

    items = []

    # recurring base: active recurring pools, at their pre-addition quantity
    for pool in frappe.get_all(
        "Customer Service",
        filters={"customer": customer, "status": "Active", "billing_mode": "Recurring"},
        fields=["name", "billing_item", "quantity", "service_label"],
    ):
        base_qty = flt(pool.quantity) - added_by_pool.get(pool.name, 0)
        if not pool.billing_item or base_qty <= 0:
            continue
        monthly = flt(_customer_rate(customer, pool.billing_item, None) / 12.0, 2)
        items.append(
            {
                "item_code": pool.billing_item,
                "qty": base_qty,
                "rate": monthly,
                "price_list_rate": monthly,
                "description": f"{pool.service_label} - recurring {posting_date[:7]}",
            }
        )

    # this period's additions
    for c in charges:
        items.append(
            {
                "item_code": c.item,
                "qty": c.qty,
                "rate": c.rate,
                "price_list_rate": c.rate,
                "description": c.description,
            }
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
    si.flags.ignore_permissions = True
    si.insert()  # draft; operator submits at month end

    for c in charges:
        frappe.db.set_value(
            "MSP Billing Charge", c.name, {"status": "Billed", "sales_invoice": si.name}
        )
    return si.name
