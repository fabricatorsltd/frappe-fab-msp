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
    if t.get("fab_billing_status") in ("Invoiced", "Subscribed", "Deferred"):
        return {"skipped": "already fulfilled"}

    rules = _rules(t)
    if rules["cmdb_effect"] == "None" and not rules["billable"]:
        return {"skipped": "nothing to do"}

    cs = _target_service(t, rules)
    result = {"customer_service": cs.name if cs else None}

    if not rules["billable"]:
        _link(t, customer_service=cs)
        return result

    rate = _customer_rate(t.customer, rules["billing_item"], cs)
    qty = int(t.get("fab_quantity") or 1)

    # the charge line for this addition, common to immediate and consolidated
    if rules["mode"] == "One-time":
        unit, desc = rate, "One-time charge"
    elif rules["coterm"] and cs and cs.renewal_date:
        days = max(date_diff(cs.renewal_date, today()), 0)
        unit, desc = flt(rate * days / 365.0, 2), f"Pro-rata to {cs.renewal_date} ({days} days)"
    else:  # recurring, new pool
        if cs and not cs.renewal_date:
            cs.db_set("renewal_date", add_days(today(), 365))
        unit, desc = rate, "Recurring (first period)"

    _apply_quantity(cs, qty)

    # consolidated customers defer: the addition goes on the monthly invoice.
    # a monthly-recurring seat needs no separate charge (it joins the monthly
    # base next run); annual and one-time additions are recorded as charges.
    if _is_consolidated(t.customer):
        charge = None
        if not (rules["mode"] == "Recurring" and rules["interval"] == "Monthly"):
            charge = _defer_charge(t, cs, rules["billing_item"], qty, unit, desc)
        _link(t, customer_service=cs, status="Deferred")
        result.update(billing_charge=charge, deferred=True)
        return result

    # immediate billing
    if rules["mode"] == "One-time":
        inv = _sales_invoice(t, rules["billing_item"], qty, unit, desc)
        _link(t, customer_service=cs, sales_invoice=inv, status="Invoiced")
        result["sales_invoice"] = inv
    else:
        inv = _sales_invoice(t, rules["billing_item"], qty, unit, desc) if (rules["coterm"] and cs and cs.renewal_date) else None
        sub = _ensure_subscription(t, cs, rules["billing_item"])
        _link(t, customer_service=cs, sales_invoice=inv, subscription=sub, status="Subscribed")
        result.update(sales_invoice=inv, subscription=sub)

    return result


def _is_consolidated(customer) -> bool:
    erp = _erp_customer(customer)
    return bool(erp) and frappe.db.get_value("Customer", erp, "fab_msp_billing_mode") == "Consolidated"


def _defer_charge(t, cs, item, qty, unit, description) -> str:
    charge = frappe.get_doc(
        {
            "doctype": "MSP Billing Charge",
            "customer": _erp_customer(t.customer),
            "posting_date": today(),
            "item": item,
            "qty": qty,
            "rate": unit,
            "description": description,
            "hd_ticket": t.name,
            "customer_service": cs.name if cs else None,
            "status": "Unbilled",
        }
    )
    charge.insert(ignore_permissions=True)
    return charge.name


def _rules(t) -> dict:
    r = {"billable": 0, "billing_item": None, "mode": "One-time", "interval": "Monthly",
         "coterm": 0, "cmdb_effect": "None", "service_type": None}
    if t.ticket_type:
        tt = frappe.get_cached_doc("HD Ticket Type", t.ticket_type)
        r.update(
            billable=tt.get("fab_billable"),
            billing_item=tt.get("fab_billing_item"),
            mode=tt.get("fab_billing_mode") or "One-time",
            interval=tt.get("fab_billing_interval") or "Monthly",
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
            "billing_mode": "Recurring" if rules["mode"] == "Recurring" else rules["mode"],
            "billing_interval": rules["interval"],
            "quantity": 0,
        }
    )
    cs.insert(ignore_permissions=True)
    return cs


def _apply_quantity(cs, qty):
    if cs:
        cs.db_set("quantity", (cs.quantity or 0) + qty)


def _customer_price_list(customer) -> str:
    return (
        (customer and frappe.db.get_value("Customer", customer, "default_price_list"))
        or frappe.db.get_single_value("Selling Settings", "selling_price_list")
        or "Standard Selling"
    )


def _customer_company(customer) -> str | None:
    """Company an invoice for this customer belongs to: the one the customer is
    restricted to, else the session default."""
    company = customer and frappe.db.get_value(
        "Allowed To Transact With", {"parent": customer, "parenttype": "Customer"}, "company"
    )
    return company or frappe.defaults.get_user_default("Company")


def _billing_currency(customer) -> str | None:
    """The currency the invoice is raised in: the customer's, else the company's."""
    company = _customer_company(customer)
    return (
        (customer and frappe.db.get_value("Customer", customer, "default_currency"))
        or (company and frappe.db.get_value("Company", company, "default_currency"))
        or frappe.db.get_default("currency")
    )


def _valid_price(item, price_list, on_date, currency, customer) -> float | None:
    """The selling price valid on a date: a row tied to this customer wins over
    a generic one, otherwise the most recently started row."""
    rows = [
        p
        for p in frappe.get_all(
            "Item Price",
            filters={"item_code": item, "selling": 1, "price_list": price_list},
            fields=["price_list_rate", "currency", "valid_from", "valid_upto", "customer"],
            order_by="valid_from desc",
        )
        if (not currency or p.currency == currency)
        and (not p.valid_from or getdate(p.valid_from) <= on_date)
        and (not p.valid_upto or getdate(p.valid_upto) >= on_date)
    ]
    if not rows:
        return None
    own = next((p for p in rows if p.customer and p.customer == customer), None)
    return flt((own or rows[0]).price_list_rate)


def _customer_rate(customer, item, cs, posting_date=None) -> float:
    """Rate from the customer's own price list, falling back to the default
    list, then the item's standard rate. Only prices valid on the posting date
    and in the invoice currency count."""
    if cs and cs.get("billing_item"):
        item = cs.billing_item or item
    if not item:
        return 0.0
    on_date = getdate(posting_date or today())
    currency = _billing_currency(customer)
    for price_list in (_customer_price_list(customer), "Standard Selling"):
        if currency and frappe.db.get_value("Price List", price_list, "currency") != currency:
            continue
        rate = _valid_price(item, price_list, on_date, currency, customer)
        if rate is not None:
            return rate
    return flt(frappe.db.get_value("Item", item, "standard_rate"))


def _sales_invoice(t, item, qty, rate, description) -> str:
    si = frappe.get_doc(
        {
            "doctype": "Sales Invoice",
            "customer": _erp_customer(t.customer),
            "ignore_pricing_rule": 1,
            "items": [
                {
                    "item_code": item,
                    "qty": qty,
                    "rate": rate,
                    "price_list_rate": rate,
                    "description": f"{item} - {description}",
                }
            ],
            "remarks": f"Auto-generated from ticket {t.name}",
        }
    )
    template = _customer_tax_template(_erp_customer(t.customer))
    if template:
        from erpnext.controllers.accounts_controller import get_taxes_and_charges

        si.taxes_and_charges = template
        for tax in get_taxes_and_charges("Sales Taxes and Charges Template", template):
            si.append("taxes", tax)
    set_payment_schedule(si)
    si.flags.ignore_permissions = True
    si.insert()  # left as draft for review
    return si.name


def default_mode_of_payment() -> str | None:
    """A mode of payment carrying an Italian MP code, needed for e-invoicing.
    Prefers Wire Transfer (MP05); otherwise the first one with a code."""
    if frappe.db.get_value("Mode of Payment", "Wire Transfer", "mode_of_payment_code"):
        return "Wire Transfer"
    for name in frappe.get_all("Mode of Payment", pluck="name"):
        if frappe.db.get_value("Mode of Payment", name, "mode_of_payment_code"):
            return name
    return None


def _payment_terms_template(si) -> str | None:
    """The template ERPNext would apply to this invoice: the customer's, else
    the company's."""
    customer = si.get("customer")
    company = si.get("company") or _customer_company(customer)
    return (
        (customer and frappe.db.get_value("Customer", customer, "payment_terms"))
        or (company and frappe.db.get_value("Company", company, "payment_terms"))
        or None
    )


def set_payment_schedule(si) -> None:
    """FatturaPA needs a mode of payment on the payment schedule.

    With a payment terms template ERPNext builds the schedule itself on validate,
    from terms that carry their own mode of payment, so we leave the template on
    the invoice and the rows to it: hand-building a single row due on the posting
    date would contradict the template and break its due-date validation. Only
    without any template do we write that row ourselves.
    """
    template = si.get("payment_terms_template") or _payment_terms_template(si)
    si.set("payment_schedule", [])
    if template:
        si.payment_terms_template = template
        return
    mop = default_mode_of_payment()
    if not mop:
        return
    si.payment_terms_template = None
    si.append(
        "payment_schedule",
        {"due_date": si.get("posting_date") or today(), "invoice_portion": 100, "mode_of_payment": mop},
    )


def _ensure_subscription(t, cs, item) -> str | None:
    customer = _erp_customer(t.customer)
    if not customer or not item:
        return None
    plan = _ensure_plan(item, _customer_price_list(t.customer))
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


def _ensure_plan(item, price_list) -> str:
    """One plan per (item, price list) so each customer's recurring price comes
    from their own list rather than a shared fixed cost."""
    existing = frappe.get_all(
        "Subscription Plan",
        filters={"item": item, "price_list": price_list},
        limit=1,
    )
    if existing:
        return existing[0].name
    plan = frappe.get_doc(
        {
            "doctype": "Subscription Plan",
            "plan_name": f"{item} @ {price_list} (annual)",
            "item": item,
            "price_determination": "Based On Price List",
            "price_list": price_list,
            "billing_interval": "Year",
            "billing_interval_count": 1,
        }
    )
    plan.flags.ignore_permissions = True
    plan.insert()
    return plan.name


def _customer_tax_template(customer) -> str | None:
    """Resolve the sales tax template for a customer.

    Uses the customer's Tax Category via Tax Rules (so a foreign customer maps to
    a 0%-with-natura template, a domestic one to standard VAT). Falls back to the
    company default, then the standard-rate (22%) template.
    """
    tax_category = customer and frappe.db.get_value("Customer", customer, "tax_category")
    if tax_category:
        by_category = frappe.db.get_value(
            "Sales Taxes and Charges Template", {"tax_category": tax_category, "disabled": 0}, "name"
        )
        if by_category:
            return by_category
        from erpnext.accounts.doctype.tax_rule.tax_rule import get_tax_template

        resolved = get_tax_template(
            today(),
            {"tax_category": tax_category, "customer": customer, "use_for_shopping_cart": 0},
        )
        if resolved:
            return resolved

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
