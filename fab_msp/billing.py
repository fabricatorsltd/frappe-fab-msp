from __future__ import annotations

import frappe
from frappe.utils import add_years, flt, getdate, today

from fab_msp.fulfillment import (
    _customer_company,
    _customer_rate,
    _customer_tax_template,
    _erp_customer,
    set_payment_schedule,
)


def reflect_invoice_on_submit(doc, method=None):
    """When an MSP-generated Sales Invoice is submitted, mark the originating
    tickets Invoiced and the field service interventions Billed. Sending to SdI
    stays the standard fab_italy_edi action on the submitted invoice."""
    from fab_msp.field_service import reflect_billed_tasks

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
    reflect_billed_tasks(doc.name)


def release_invoice_charges(doc, method=None):
    """A cancelled invoice, or a deleted draft, frees what it carried.

    The charges go back to Unbilled so the next run picks them up, and the
    interventions behind them back to "To bill". Tickets keep their status: the
    invoice they point at still exists as a cancelled document.
    """
    from fab_msp.field_service import release_billed_tasks

    charges = frappe.get_all(
        "MSP Billing Charge", filters={"sales_invoice": doc.name}, fields=["name", "task"]
    )
    for charge in charges:
        frappe.db.set_value(
            "MSP Billing Charge", charge.name, {"status": "Unbilled", "sales_invoice": None}
        )
    release_billed_tasks({c.task for c in charges if c.task})


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


def _is_annual(pool) -> bool:
    return (pool.get("billing_interval") or "Monthly") == "Annual"


def _added_by_pool(charges) -> dict[str, float]:
    """Seats added this period, per pool: they come in as their own lines."""
    added: dict[str, float] = {}
    for c in charges:
        if c.get("customer_service"):
            added[c["customer_service"]] = added.get(c["customer_service"], 0) + flt(c["qty"])
    return added


def _base_qty(pool, added_by_pool) -> float:
    return flt(pool["quantity"]) - added_by_pool.get(pool["name"], 0)


def _bills_this_period(pool, posting_date) -> bool:
    """Monthly pools bill every month, annual ones only in their renewal month."""
    if not _is_annual(pool):
        return True
    return bool(pool.get("renewal_date")) and str(pool["renewal_date"])[5:7] == posting_date[5:7]


def annual_pools_billed(posting_date, pools, charges) -> list[str]:
    """The annual pools that get a base line this period, so their renewal date
    can be moved on a year once the invoice exists."""
    added_by_pool = _added_by_pool(charges)
    return [
        p["name"]
        for p in pools
        if _is_annual(p)
        and _bills_this_period(p, posting_date)
        and p.get("billing_item")
        and _base_qty(p, added_by_pool) > 0
    ]


def build_consolidated_items(posting_date, pools, charges, rate_fn) -> list[dict]:
    """Pure line builder for a consolidated invoice.

    - monthly services bill every month; annual only in their renewal month
      (matched by month-of-year);
    - monthly lines come first, annual ones after, each group keeping the order
      the pools were given in;
    - the base excludes seats added this period (they arrive as addition lines);
    - deferred additions are appended as their own lines.
    """
    added_by_pool = _added_by_pool(charges)
    items: list[dict] = []
    for pool in sorted(pools, key=_is_annual):
        base_qty = _base_qty(pool, added_by_pool)
        if not pool.get("billing_item") or base_qty <= 0:
            continue
        if not _bills_this_period(pool, posting_date):
            continue
        rate = flt(rate_fn(pool["billing_item"]))
        if _is_annual(pool):
            description = f"{pool['service_label']} - annual {posting_date[:4]}"
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


def _warn_on_fallback_tax(customer, template) -> None:
    """No tax category means the company default template, which is standard VAT.

    Harmless for an Italian customer, wrong for a foreign one, so a foreign
    customer landing there leaves a trace instead of a quietly taxed invoice.
    """
    if frappe.db.get_value("Customer", customer, "tax_category"):
        return
    address = frappe.db.get_value(
        "Dynamic Link",
        {"link_doctype": "Customer", "link_name": customer, "parenttype": "Address"},
        "parent",
    )
    country = address and frappe.db.get_value("Address", address, "country")
    if not country or country == "Italy":
        return
    frappe.log_error(
        title="MSP: foreign customer billed with the default tax template",
        message=f"Customer {customer} ({country}) has no Tax Category, so {template} was used. "
        "Set the customer's tax category to a 0% template.",
    )


@frappe.whitelist()
def generate_for_customer(customer: str, posting_date: str | None = None) -> str | None:
    """One draft invoice for a customer: recurring base of active pools plus the
    period's deferred additions. The base excludes the seats added this period
    (they come in as their own addition lines) so nothing is billed twice.

    One invoice per customer and period: the remark is the key, so a second run
    for the same period returns the invoice already there."""
    posting_date = posting_date or today()
    erp = _erp_customer(customer)
    remark = f"MSP consolidated invoice {posting_date[:7]}"

    existing = frappe.db.get_value(
        "Sales Invoice", {"customer": erp, "remarks": remark, "docstatus": ["<", 2]}, "name"
    )
    if existing:
        return existing

    charges = [
        dict(c)
        for c in frappe.get_all(
            "MSP Billing Charge",
            filters={"customer": erp, "status": "Unbilled"},
            fields=["name", "item", "qty", "rate", "description", "customer_service"],
        )
    ]
    pools = [
        dict(p)
        for p in frappe.get_all(
            "Customer Service",
            filters={"customer": customer, "status": "Active", "billing_mode": "Recurring"},
            fields=["name", "billing_item", "quantity", "service_label", "billing_interval", "renewal_date"],
            order_by="creation asc",
        )
    ]
    items = build_consolidated_items(
        posting_date,
        pools,
        charges,
        lambda item: _customer_rate(customer, item, None, posting_date),
    )
    if not items:
        return None

    si = frappe.get_doc(
        {
            "doctype": "Sales Invoice",
            "customer": erp,
            "company": _customer_company(erp),
            "posting_date": posting_date,
            "set_posting_time": 1,  # keep the requested date, ERPNext defaults to today
            "ignore_pricing_rule": 1,
            # the site default language would otherwise win over the customer's,
            # and a foreign customer gets their invoice in their own language
            "language": frappe.db.get_value("Customer", erp, "language"),
            "items": items,
            "remarks": remark,
        }
    )
    template = _customer_tax_template(erp)
    _warn_on_fallback_tax(erp, template)
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
            "MSP Billing Charge", c["name"], {"status": "Billed", "sales_invoice": si.name}
        )
    from fab_msp.field_service import link_tasks_to_invoice

    link_tasks_to_invoice(si.name)
    # an annual pool is due again a year after the period just billed
    for pool in annual_pools_billed(posting_date, pools, charges):
        renewal = frappe.db.get_value("Customer Service", pool, "renewal_date")
        if renewal:
            frappe.db.set_value("Customer Service", pool, "renewal_date", add_years(getdate(renewal), 1))
    return si.name
