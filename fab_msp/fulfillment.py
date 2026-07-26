from __future__ import annotations

import frappe
from frappe.utils import add_days, date_diff, flt, getdate, today


def fulfill_ticket(ticket_name: str) -> dict:
    """Provision CMDB and ERP billing for an approved service request.

    Runs as a system operation: the trigger may be a customer manager who has no
    rights on accounts/invoices, but the billing artefacts are created by the
    system. Idempotent per ticket.
    """
    original_user = frappe.session.user
    try:
        frappe.set_user("Administrator")
        return _fulfill(ticket_name)
    finally:
        frappe.set_user(original_user)


def _fulfill(ticket_name: str) -> dict:
    t = frappe.get_doc("HD Ticket", ticket_name)
    if t.get("fab_sales_invoice") or t.get("fab_subscription"):
        return {"skipped": "already fulfilled"}

    rules = _rules(t)
    if rules["cmdb_effect"] == "None" and not rules["billable"]:
        return {"skipped": "nothing to do"}

    cs = _target_service(t, rules)
    result = {"customer_service": cs.name if cs else None}

    if not rules["billable"]:
        _link(t, customer_service=cs)
        return result

    rate = _annual_rate(rules["billing_item"], cs)
    qty = 1

    if rules["mode"] == "One-time":
        inv = _sales_invoice(t, rules["billing_item"], qty, rate, "One-time charge")
        _apply_quantity(cs, qty)
        _link(t, customer_service=cs, sales_invoice=inv, status="Invoiced")
        result["sales_invoice"] = inv

    elif rules["coterm"] and cs and cs.renewal_date:
        days = max(date_diff(cs.renewal_date, today()), 0)
        unit = flt(rate * days / 365.0, 2)
        inv = _sales_invoice(
            t, rules["billing_item"], qty, unit,
            f"Pro-rata to {cs.renewal_date} ({days} days)",
        )
        _apply_quantity(cs, qty)  # joins the pool; recurring picks it up at renewal
        sub = _ensure_subscription(t, cs, rules["billing_item"], rate)
        _link(t, customer_service=cs, sales_invoice=inv, subscription=sub, status="Subscribed")
        result.update(sales_invoice=inv, subscription=sub, prorata_unit=unit, days=days)

    else:  # plain recurring, new pool
        if cs and not cs.renewal_date:
            cs.db_set("renewal_date", add_days(today(), 365))
        _apply_quantity(cs, qty)
        sub = _ensure_subscription(t, cs, rules["billing_item"], rate)
        _link(t, customer_service=cs, subscription=sub, status="Subscribed")
        result["subscription"] = sub

    return result


def _rules(t) -> dict:
    r = {"billable": 0, "billing_item": None, "mode": "One-time", "coterm": 0,
         "cmdb_effect": "None", "service_type": None}
    if t.ticket_type:
        tt = frappe.get_cached_doc("HD Ticket Type", t.ticket_type)
        r.update(
            billable=tt.get("fab_billable"),
            billing_item=tt.get("fab_billing_item"),
            mode=tt.get("fab_billing_mode") or "One-time",
            coterm=tt.get("fab_coterm_prorate"),
            cmdb_effect=tt.get("fab_cmdb_effect") or "None",
            service_type=tt.get("fab_service_type"),
        )
    return r


def _target_service(t, rules):
    """The service the request acts on: the linked one, an existing pool to
    co-terminate into, or a freshly created service."""
    if t.get("fab_customer_service"):
        return frappe.get_doc("Customer Service", t.fab_customer_service)
    if rules["coterm"] and rules["billing_item"] and t.customer:
        pool = frappe.get_all(
            "Customer Service",
            filters={"customer": t.customer, "billing_item": rules["billing_item"], "status": "Active"},
            order_by="creation asc",
            limit=1,
        )
        if pool:
            return frappe.get_doc("Customer Service", pool[0].name)
    if rules["cmdb_effect"] == "Create" and rules["service_type"] and t.customer:
        return _create_service(t, rules)
    return None


def _create_service(t, rules):
    cs = frappe.get_doc(
        {
            "doctype": "Customer Service",
            "customer": t.customer,
            "service_type": rules["service_type"],
            "service_label": t.subject or f"Service for {t.name}",
            "billing_item": rules["billing_item"],
            "quantity": 0,
        }
    )
    cs.insert(ignore_permissions=True)
    return cs


def _apply_quantity(cs, qty):
    if cs:
        cs.db_set("quantity", (cs.quantity or 0) + qty)


def _annual_rate(item, cs) -> float:
    if cs and cs.get("billing_item"):
        item = cs.billing_item or item
    if not item:
        return 0.0
    price = frappe.get_all(
        "Item Price",
        filters={"item_code": item, "selling": 1},
        fields=["price_list_rate"],
        order_by="valid_from desc",
        limit=1,
    )
    if price:
        return flt(price[0].price_list_rate)
    return flt(frappe.db.get_value("Item", item, "standard_rate"))


def _sales_invoice(t, item, qty, rate, description) -> str:
    si = frappe.get_doc(
        {
            "doctype": "Sales Invoice",
            "customer": _erp_customer(t.customer),
            "items": [
                {
                    "item_code": item,
                    "qty": qty,
                    "rate": rate,
                    "description": f"{item} - {description}",
                }
            ],
            "remarks": f"Auto-generated from ticket {t.name}",
        }
    )
    template = _sales_tax_template()
    if template:
        from erpnext.controllers.accounts_controller import get_taxes_and_charges

        si.taxes_and_charges = template
        for tax in get_taxes_and_charges("Sales Taxes and Charges Template", template):
            si.append("taxes", tax)
    si.flags.ignore_permissions = True
    si.insert()  # left as draft for review
    return si.name


def _ensure_subscription(t, cs, item, rate) -> str | None:
    customer = _erp_customer(t.customer)
    if not customer or not item:
        return None
    plan = _ensure_plan(item, rate)
    existing = frappe.get_all(
        "Subscription",
        filters={"party_type": "Customer", "party": customer, "status": ["!=", "Cancelled"]},
        limit=1,
    )
    if existing:
        sub = frappe.get_doc("Subscription", existing[0].name)
        row = next((p for p in sub.plans if p.plan == plan), None)
        if row:
            row.qty = (row.qty or 0) + 1
        else:
            sub.append("plans", {"plan": plan, "qty": 1})
        sub.flags.ignore_permissions = True
        sub.save()
        return sub.name
    sub = frappe.get_doc(
        {
            "doctype": "Subscription",
            "party_type": "Customer",
            "party": customer,
            "start_date": today(),
            "plans": [{"plan": plan, "qty": 1}],
        }
    )
    sub.flags.ignore_permissions = True
    sub.insert()
    return sub.name


def _ensure_plan(item, rate) -> str:
    name = frappe.db.get_value("Subscription Plan", {"item": item}, "name")
    if name:
        return name
    plan = frappe.get_doc(
        {
            "doctype": "Subscription Plan",
            "plan_name": f"{item} (annual)",
            "item": item,
            "price_determination": "Fixed Rate",
            "cost": flt(rate),
            "billing_interval": "Year",
            "billing_interval_count": 1,
        }
    )
    plan.flags.ignore_permissions = True
    plan.insert()
    return plan.name


def _sales_tax_template() -> str | None:
    """Pick the sales tax template for auto-generated invoices.

    Prefers the company default; otherwise the standard-rate (22%) template.
    TODO: make this configurable per Customer Service Type for mixed rates.
    """
    template = frappe.db.get_value(
        "Sales Taxes and Charges Template", {"is_default": 1, "disabled": 0}, "name"
    )
    if template:
        return template
    candidates = frappe.get_all(
        "Sales Taxes and Charges Template", filters={"disabled": 0}, pluck="name"
    )
    return next((c for c in candidates if "22" in c), candidates[0] if candidates else None)


def _erp_customer(hd_customer):
    """HD Ticket.customer is an HD Customer; ERPNext billing needs a Customer.
    They share the name in this instance; fall back to the HD Customer name."""
    if hd_customer and frappe.db.exists("Customer", hd_customer):
        return hd_customer
    return hd_customer


def _link(t, customer_service=None, sales_invoice=None, subscription=None, status=None):
    values = {}
    if customer_service:
        values["fab_customer_service"] = customer_service.name
    if sales_invoice:
        values["fab_sales_invoice"] = sales_invoice
    if subscription:
        values["fab_subscription"] = subscription
    if status:
        values["fab_billing_status"] = status
    if values:
        t.db_set(values)
    if subscription and customer_service:
        customer_service.db_set("subscription", subscription)
