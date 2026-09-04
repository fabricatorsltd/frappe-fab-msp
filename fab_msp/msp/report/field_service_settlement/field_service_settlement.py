"""Pay-when-paid: which technician invoices may be paid because Trison has paid.

One row per completed intervention, with what we billed Trison, the state of that
invoice, and the technician's own invoice against it.

"Billed" is the sum of the charges the intervention produced, not a recomputation
from the task, so a corrected task after the invoice went out cannot make the row
disagree with what was invoiced. "Payable now" reads the consolidated invoice as a
whole: an invoice only partly paid still counts as unpaid.
"""

import frappe
from frappe import _


def execute(filters=None):
    filters = frappe._dict(filters or {})
    return get_columns(), get_data(filters)


def get_columns():
    return [
        {"label": _("Parte"), "fieldname": "parte", "fieldtype": "Data", "width": 110},
        {"label": _("Task"), "fieldname": "task", "fieldtype": "Link", "options": "Task", "width": 130},
        {"label": _("Date"), "fieldname": "intervention_date", "fieldtype": "Date", "width": 95},
        {"label": _("Technician"), "fieldname": "technician", "fieldtype": "Data", "width": 190},
        {"label": _("Kind"), "fieldname": "technician_kind", "fieldtype": "Data", "width": 80},
        {"label": _("Billed"), "fieldname": "billed", "fieldtype": "Currency", "width": 100},
        {
            "label": _("Sales Invoice"),
            "fieldname": "sales_invoice",
            "fieldtype": "Link",
            "options": "Sales Invoice",
            "width": 140,
        },
        {"label": _("Invoice Status"), "fieldname": "invoice_status", "fieldtype": "Data", "width": 110},
        {
            "label": _("Technician Invoice"),
            "fieldname": "purchase_invoice",
            "fieldtype": "Link",
            "options": "Purchase Invoice",
            "width": 150,
        },
        {"label": _("Outstanding"), "fieldname": "outstanding", "fieldtype": "Currency", "width": 110},
        {"label": _("Payable Now"), "fieldname": "payable_now", "fieldtype": "Check", "width": 100},
    ]


def get_data(filters):
    conditions = ""
    if filters.get("from_date"):
        conditions += " and t.fab_fs_intervention_date >= %(from_date)s"
    if filters.get("to_date"):
        conditions += " and t.fab_fs_intervention_date <= %(to_date)s"

    rows = frappe.db.sql(
        f"""
        select
            t.fab_fs_parte as parte,
            t.name as task,
            t.fab_fs_intervention_date as intervention_date,
            coalesce(s.supplier_name, e.employee_name, '') as technician,
            t.fab_fs_technician_kind as technician_kind,
            (select sum(c.qty * c.rate) from `tabMSP Billing Charge` c where c.task = t.name) as billed,
            t.fab_fs_sales_invoice as sales_invoice,
            si.status as invoice_status,
            t.fab_fs_purchase_invoice as purchase_invoice,
            pi.outstanding_amount as outstanding,
            case when si.status = 'Paid' and pi.outstanding_amount > 0 then 1 else 0 end as payable_now
        from `tabTask` t
        left join `tabSupplier` s on s.name = t.fab_fs_supplier
        left join `tabEmployee` e on e.name = t.fab_fs_employee
        left join `tabSales Invoice` si on si.name = t.fab_fs_sales_invoice
        left join `tabPurchase Invoice` pi on pi.name = t.fab_fs_purchase_invoice
        where ifnull(t.fab_fs_parte, '') != '' and t.status = 'Completed' {conditions}
        order by t.fab_fs_intervention_date desc, t.fab_fs_parte desc
        """,
        filters,
        as_dict=True,
    )
    if filters.get("payable_only"):
        rows = [r for r in rows if r.payable_now]
    return rows
