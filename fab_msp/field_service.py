"""On-site field service on ERPNext Tasks: the TRISON parte de ticket.

A Task under the TRISON project is one intervention. It carries the data of the
Trison form (parte, end customer, times, materials, travel), prints back the same
form, and on close turns into the two consolidated-billing charges we invoice
Trison with: a call-out and the hours.
"""

from __future__ import annotations

import math
import os

import frappe
from frappe import _
from frappe.utils import flt, get_time, getdate, today

from fab_msp import esignature

CALLOUT_ITEM = "Field service call-out"
HOUR_ITEM = "Field service hour"
DEFAULT_CALLOUT_FEE = 40
DEFAULT_HOURLY_RATE = 40

# what the invoice line calls each of the two charges, in the customer's language
LINE_WORDS = {
    "en": {CALLOUT_ITEM: "call-out", HOUR_ITEM: "labour"},
    "es": {CALLOUT_ITEM: "desplazamiento", HOUR_ITEM: "mano de obra"},
}

FIELD_TECHNICIAN_ROLE = "Field Technician"
# roles that run the jobs and the money, so never narrowed to one supplier
UNRESTRICTED_ROLES = {"System Manager", "Projects Manager", "Accounts Manager"}
# these get the task itself (permlevel 0) and the money on it (permlevel 1)
OFFICE_ROLES = ("Accounts Manager", "Projects Manager")

TRISON_CUSTOMER = "TRISON EUROPE, S.L.U."
TRISON_PROJECT = "TRISON field service"

PRINT_FORMAT = "Parte de Ticket TRISON"
PDF_GENERATOR = "chrome"
TEMPLATE = os.path.join(os.path.dirname(__file__), "print_formats", "trison_parte_de_ticket.html")


# --- pure logic ------------------------------------------------------------


def billable_hours(arrival, departure) -> float:
    """On-site time rounded up to the next half hour.

    A call from 10:00 to 11:10 bills 1.5 h. A departure earlier than the arrival
    is a job running past midnight, so it counts on the next day. Travel is paid
    by the call-out fee and stays out of the billed hours.
    """
    minutes = _minutes_between(arrival, departure)
    if minutes <= 0:
        return 0.0
    return math.ceil(minutes / 30.0) / 2.0


def _minutes_between(arrival, departure) -> float:
    if not arrival or not departure:
        return 0.0
    start, end = _minutes_of_day(arrival), _minutes_of_day(departure)
    if end < start:
        end += 24 * 60  # past midnight
    return end - start


def _minutes_of_day(value) -> float:
    value = get_time(value)
    return value.hour * 60 + value.minute + value.second / 60.0


def charge_label(task) -> str:
    """Parte, date and end customer, so a consolidated invoice line says which
    job it is without opening anything."""
    date = task.get("fab_fs_intervention_date")
    parts = [
        task.get("fab_fs_parte"),
        getdate(date).strftime("%d/%m/%Y") if date else None,
        task.get("fab_fs_end_customer"),
    ]
    return " - ".join(p for p in parts if p)


def build_charges(task, language=None) -> list[dict]:
    """The lines a closed intervention bills: the call-out and the hours.

    The fee and the rate fall back to the module defaults when the task carries
    none, so an emptied field never bills zero.
    """
    label = charge_label(task)
    words = LINE_WORDS.get((language or "en")[:2], LINE_WORDS["en"])
    lines = [
        {
            "item": CALLOUT_ITEM,
            "qty": 1,
            "rate": flt(task.get("fab_fs_callout_fee")) or DEFAULT_CALLOUT_FEE,
            "description": f"{label} - {words[CALLOUT_ITEM]}",
        }
    ]
    hours = flt(task.get("fab_fs_hours_billed"))
    if hours > 0:
        lines.append(
            {
                "item": HOUR_ITEM,
                "qty": hours,
                "rate": flt(task.get("fab_fs_hourly_rate")) or DEFAULT_HOURLY_RATE,
                "description": f"{label} - {words[HOUR_ITEM]}",
            }
        )
    return lines


# --- print format ----------------------------------------------------------

MATERIAL_ROWS = 5
LABOUR_ROWS = 4
TRAVEL_ROWS = 4
TECHNICIAN_CATEGORY = "Técnico"


def trison_parte(doc) -> dict:
    """Everything the Trison form prints that is not a plain field on the task.

    Kept in python so the print format stays layout only, and so the fixed number
    of empty rows of the paper form is produced in one place.
    """
    date = doc.get("fab_fs_intervention_date")
    date = getdate(date).strftime("%d/%m/%Y") if date else ""
    operator = operator_name(doc)
    used = list(doc.get("fab_fs_materials_used") or [])
    removed = list(doc.get("fab_fs_materials_removed") or [])
    labour = [
        {
            "date": date,
            "operator": operator,
            "category": TECHNICIAN_CATEGORY,
            "travel": _hours(doc.get("fab_fs_travel_time")),
            "arrival": _hhmm(doc.get("fab_fs_arrival_time")),
            "departure": _hhmm(doc.get("fab_fs_departure_time")),
            "task": doc.get("fab_fs_work_performed") or "",
        }
    ]
    return {
        "date": date,
        "operator": operator,
        "material": [
            {"used": _at(used, i), "removed": _at(removed, i)}
            for i in range(max(MATERIAL_ROWS, len(used), len(removed)))
        ],
        "labour": _pad(labour, LABOUR_ROWS),
        "travel": _pad(list(doc.get("fab_fs_travel") or []), TRAVEL_ROWS),
    }


def operator_name(doc) -> str:
    if doc.get("fab_fs_technician_kind") == "External" and doc.get("fab_fs_supplier"):
        return frappe.db.get_value("Supplier", doc.fab_fs_supplier, "supplier_name") or ""
    if doc.get("fab_fs_employee"):
        return frappe.db.get_value("Employee", doc.fab_fs_employee, "employee_name") or ""
    return ""


def _at(rows, index):
    return rows[index] if index < len(rows) else None


def _pad(rows, size):
    return rows + [None] * max(0, size - len(rows))


def _hhmm(value) -> str:
    return get_time(value).strftime("%H:%M") if value else ""


def _hours(value) -> str:
    value = flt(value)
    return f"{value:.2f}".rstrip("0").rstrip(".") + " h" if value else ""


# --- document events -------------------------------------------------------


def apply_field_service_defaults(doc, method=None):
    """Fill and check what the parte can settle before the save. Only for field
    service tasks."""
    if not doc.get("fab_fs_parte"):
        return
    _validate_unique_parte(doc)
    _validate_billable(doc)
    _set_hours_billed(doc)
    _set_travel_totals(doc)
    _set_technician_user(doc)
    if not doc.get("fab_fs_billing_status"):
        doc.fab_fs_billing_status = "Not billable"


def _validate_unique_parte(doc):
    other = frappe.db.get_value(
        "Task", {"fab_fs_parte": doc.fab_fs_parte, "name": ["!=", doc.name]}, "name"
    )
    if other:
        frappe.throw(_("Parte {0} is already on task {1}.").format(doc.fab_fs_parte, other))


def _validate_billable(doc):
    """Closing bills the intervention, so refuse the close here rather than in
    on_update, where a throw would roll the whole save back."""
    if doc.get("status") != "Completed" or _project_customer(doc):
        return
    frappe.throw(
        _("Set a customer on project {0} before completing intervention {1}.").format(
            doc.get("project") or _("(none)"), doc.fab_fs_parte
        )
    )


def _project_customer(doc):
    return frappe.db.get_value("Project", doc.project, "customer") if doc.get("project") else None


def _set_hours_billed(doc):
    """The billed hours default to the time on site.

    They are recomputed only when the times moved in this same save and the
    operator did not type hours of their own, so a manual override survives.
    """
    arrival, departure = doc.get("fab_fs_arrival_time"), doc.get("fab_fs_departure_time")
    if doc.is_new():
        if not flt(doc.get("fab_fs_hours_billed")):
            doc.fab_fs_hours_billed = billable_hours(arrival, departure)
        return
    times_changed = doc.has_value_changed("fab_fs_arrival_time") or doc.has_value_changed(
        "fab_fs_departure_time"
    )
    if not times_changed or doc.has_value_changed("fab_fs_hours_billed"):
        return
    doc.fab_fs_hours_billed = billable_hours(arrival, departure) if (arrival or departure) else 0


def _set_travel_totals(doc):
    for row in doc.get("fab_fs_travel") or []:
        if not flt(row.total):
            row.total = flt(row.parking) + flt(row.toll)


def _set_technician_user(doc):
    """An external technician's desk user is the one restricted to that supplier,
    so the parte and the reports name a person without asking for it twice."""
    if doc.get("fab_fs_technician_user") or doc.get("fab_fs_technician_kind") != "External":
        return
    if not doc.get("fab_fs_supplier"):
        return
    users = frappe.get_all(
        "User Permission",
        filters={"allow": "Supplier", "for_value": doc.fab_fs_supplier},
        pluck="user",
    )
    if len(users) == 1:
        doc.fab_fs_technician_user = users[0]


def maybe_bill_on_close(doc, method=None):
    """Completing a task with a parte bills the intervention; later edits to the
    hours or the fees reach the charges that are not on an invoice yet."""
    if not doc.get("fab_fs_parte"):
        return
    if doc.get("status") == "Completed":
        bill_intervention(doc)
    sync_unbilled_charges(doc)


def bill_intervention(doc) -> list[str]:
    """Turn a closed intervention into its consolidated-billing charges.

    Idempotent: the charges already linked to the task are returned untouched, so
    closing twice never bills twice. The task row is locked first, so two saves
    landing together cannot both decide there is nothing there yet.
    """
    customer = _project_customer(doc)
    if not customer:
        return []  # refused at validate; never bill against nobody

    frappe.db.get_value("Task", doc.name, "name", for_update=True)
    existing = frappe.get_all("MSP Billing Charge", filters={"task": doc.name}, pluck="name")
    if existing:
        return existing

    from fab_msp.fulfillment import defer_charge

    names = [
        defer_charge(
            customer,
            line["item"],
            line["qty"],
            line["rate"],
            line["description"],
            posting_date=doc.get("fab_fs_intervention_date") or today(),
            task=doc.name,
        )
        for line in build_charges(doc, _customer_language(customer))
    ]
    doc.db_set("fab_fs_billing_status", "To bill")
    _link_charges(doc)
    return names


def sync_unbilled_charges(doc) -> None:
    """Keep the charges of an intervention in step with the task.

    Correcting the hours, a fee or the end customer after the close has to reach
    the invoice. A charge already carried onto an invoice (Billed) is frozen.
    """
    charges = frappe.get_all(
        "MSP Billing Charge", filters={"task": doc.name}, fields=["name", "item", "status"]
    )
    if not charges:
        return
    customer = _project_customer(doc)
    lines = {line["item"]: line for line in build_charges(doc, _customer_language(customer))}
    stale = []
    for charge in charges:
        line = lines.pop(charge.item, None)
        if charge.status != "Unbilled":
            continue
        if line is None:
            stale.append(charge.name)
            continue
        frappe.db.set_value(
            "MSP Billing Charge",
            charge.name,
            {"qty": line["qty"], "rate": line["rate"], "description": line["description"]},
        )
    if stale:
        # the task still links what is about to go; _link_charges puts back what stays
        doc.db_set({"fab_fs_callout_charge": None, "fab_fs_labour_charge": None})
        for name in stale:
            frappe.delete_doc("MSP Billing Charge", name, ignore_permissions=True)
    if customer:
        from fab_msp.fulfillment import defer_charge

        for line in lines.values():  # a line that only appeared later, e.g. hours typed after close
            defer_charge(
                customer,
                line["item"],
                line["qty"],
                line["rate"],
                line["description"],
                posting_date=doc.get("fab_fs_intervention_date") or today(),
                task=doc.name,
            )
    _link_charges(doc)


def _link_charges(doc) -> None:
    charges = {
        c.item: c.name
        for c in frappe.get_all("MSP Billing Charge", filters={"task": doc.name}, fields=["name", "item"])
    }
    values = {
        "fab_fs_callout_charge": charges.get(CALLOUT_ITEM),
        "fab_fs_labour_charge": charges.get(HOUR_ITEM),
    }
    if any(doc.get(k) != v for k, v in values.items()):
        doc.db_set(values)


def _customer_language(customer) -> str | None:
    return frappe.db.get_value("Customer", customer, "language") if customer else None


def link_tasks_to_invoice(invoice: str) -> None:
    """Record which draft an intervention landed on, without calling it billed:
    the settlement report can then say "on FATT/..., still Draft"."""
    for task in frappe.get_all(
        "MSP Billing Charge", filters={"sales_invoice": invoice}, pluck="task"
    ):
        if task:
            frappe.db.set_value("Task", task, "fab_fs_sales_invoice", invoice)


def reflect_billed_tasks(invoice: str) -> None:
    """The interventions on a submitted invoice are billed."""
    for task in frappe.get_all(
        "MSP Billing Charge", filters={"sales_invoice": invoice}, pluck="task"
    ):
        if task:
            frappe.db.set_value(
                "Task", task, {"fab_fs_billing_status": "Billed", "fab_fs_sales_invoice": invoice}
            )


def release_billed_tasks(tasks) -> None:
    """A cancelled or deleted invoice puts its interventions back in the queue."""
    for task in tasks:
        if task and frappe.db.exists("Task", task):
            frappe.db.set_value(
                "Task", task, {"fab_fs_billing_status": "To bill", "fab_fs_sales_invoice": None}
            )


def force_private_attachment(doc, method=None):
    """A parte carries the end customer's data, so nothing attached to a Task is
    public.

    Frappe writes the file inside File.before_insert, before any hook of ours can
    run, so the file is moved afterwards (File.validate relocates it when
    is_private changes) and the field that pointed at the public URL follows.
    """
    if doc.is_folder or doc.is_private or doc.is_remote_file:
        return
    if doc.attached_to_doctype != "Task":
        return
    public_url = doc.file_url
    doc.is_private = 1
    doc.save(ignore_permissions=True)
    field = doc.attached_to_field
    if not field or not doc.attached_to_name or not frappe.get_meta("Task").has_field(field):
        return
    if frappe.db.get_value("Task", doc.attached_to_name, field) == public_url:
        frappe.db.set_value("Task", doc.attached_to_name, field, doc.file_url)


# --- technician visibility -------------------------------------------------


def _restricted_technician(user) -> bool:
    if user == "Administrator":
        return False
    roles = set(frappe.get_roles(user))
    return FIELD_TECHNICIAN_ROLE in roles and not (UNRESTRICTED_ROLES & roles)


def _allowed_suppliers(user) -> list[str]:
    return frappe.get_all(
        "User Permission", filters={"user": user, "allow": "Supplier"}, pluck="for_value"
    )


def task_query_conditions(user=None, doctype=None):
    """A field technician's task list holds their own jobs only.

    User Permissions already deny another technician's task, but with strict user
    permissions off they also let through every task with no supplier at all, so
    the list is narrowed here.
    """
    user = user or frappe.session.user
    if not _restricted_technician(user):
        return ""
    suppliers = _allowed_suppliers(user)
    if suppliers:
        values = ", ".join(frappe.db.escape(s) for s in suppliers)
        return f"(`tabTask`.`fab_fs_supplier` in ({values}))"
    return f"(`tabTask`.`fab_fs_technician_user` = {frappe.db.escape(user)})"


def task_has_permission(doc, ptype=None, user=None):
    user = user or frappe.session.user
    if not _restricted_technician(user):
        return True
    suppliers = _allowed_suppliers(user)
    if suppliers:
        return doc.get("fab_fs_supplier") in suppliers
    return bool(doc.get("fab_fs_technician_user")) and doc.get("fab_fs_technician_user") == user


# --- setup -----------------------------------------------------------------


def ensure_field_service_setup():
    """Everything the field service module needs on a site, besides the custom
    fields that install.ensure_custom_fields creates. Idempotent."""
    if not frappe.db.exists("DocType", "Task"):
        return
    ensure_service_items()
    ensure_technician_role()
    ensure_task_permissions()
    ensure_print_format()
    ensure_trison_setup()


def ensure_service_items():
    """The two things Trison is billed for."""
    group = "Services" if frappe.db.exists("Item Group", "Services") else "All Item Groups"
    specs = [
        {"item_code": CALLOUT_ITEM, "stock_uom": "Nos", "standard_rate": DEFAULT_CALLOUT_FEE},
        {"item_code": HOUR_ITEM, "stock_uom": "Hour", "standard_rate": DEFAULT_HOURLY_RATE},
    ]
    for spec in specs:
        if frappe.db.exists("Item", spec["item_code"]):
            continue
        item = frappe.get_doc(
            {
                "doctype": "Item",
                "item_name": spec["item_code"],
                "item_group": group,
                "is_stock_item": 0,
                "is_sales_item": 1,
                "is_purchase_item": 0,
                **spec,
            }
        )
        item.insert(ignore_permissions=True)


def ensure_technician_role():
    if frappe.db.exists("Role", FIELD_TECHNICIAN_ROLE):
        return
    role = frappe.get_doc({"doctype": "Role", "role_name": FIELD_TECHNICIAN_ROLE, "desk_access": 1})
    role.insert(ignore_permissions=True)


def ensure_task_permissions():
    """Who reads a task, and who reads the money on it.

    Technicians get the task at permlevel 0; the fee, the rate, the billing state
    and the invoices live at permlevel 1, which only the office roles hold.
    Accounting and project managers also need the task itself (permlevel 0, with
    `report` for the settlement report): ERPNext ships Task perms for Projects
    User and the HR roles only. `add_permission` copies the standard perms into
    Custom DocPerm the first time (frappe.permissions.setup_custom_perms), after
    which the standard ones no longer apply; on this site Task was already
    customised, so nothing is shadowed that was not already.
    """
    from frappe.permissions import add_permission, update_permission_property

    changed = False

    def grant(role, permlevel, ptypes):
        nonlocal changed
        if frappe.db.exists(
            "Custom DocPerm", {"parent": "Task", "role": role, "permlevel": permlevel}
        ):
            return
        add_permission("Task", role, permlevel)
        for ptype in ptypes:
            update_permission_property("Task", role, permlevel, ptype, 1, validate=False)
        changed = True

    grant(FIELD_TECHNICIAN_ROLE, 0, ("write", "print", "email"))
    for role in OFFICE_ROLES:
        grant(role, 0, ("write", "report", "print", "export"))
        grant(role, 1, ("write",))
    grant("Projects User", 1, ("write",))

    if changed:
        frappe.clear_cache(doctype="Task")


def ensure_print_format():
    """Create or refresh the parte from the template file. Rendered by Chrome."""
    with open(TEMPLATE, encoding="utf-8") as f:
        html = f.read()
    values = {
        "doc_type": "Task",
        "module": "MSP",
        "standard": "No",
        "custom_format": 1,
        "print_format_type": "Jinja",
        "disabled": 0,
        "default_print_language": "es",
        "pdf_generator": PDF_GENERATOR,
        "html": html,
    }
    if frappe.db.exists("Print Format", PRINT_FORMAT):
        doc = frappe.get_doc("Print Format", PRINT_FORMAT)
        if any(doc.get(k) != v for k, v in values.items()):
            doc.update(values)
            doc.save(ignore_permissions=True)
    else:
        doc = frappe.get_doc({"doctype": "Print Format", "name": PRINT_FORMAT, **values})
        doc.insert(ignore_permissions=True)


def ensure_trison_setup() -> str | None:
    """The Trison customer and the project its interventions hang under.

    Only on a site that already has the customer. The customer values are written
    when the project is created, and afterwards only into fields still empty, so
    a later migrate never overwrites what an operator changed.
    """
    if not frappe.db.exists("Customer", TRISON_CUSTOMER):
        return None

    project = frappe.db.get_value("Project", {"project_name": TRISON_PROJECT}, "name")
    first_run = not project
    if first_run:
        doc = frappe.get_doc(
            {
                "doctype": "Project",
                "project_name": TRISON_PROJECT,
                "customer": TRISON_CUSTOMER,
                "status": "Open",
            }
        )
        doc.insert(ignore_permissions=True)
        project = doc.name

    _seed_trison_customer(first_run)
    return project


def _seed_trison_customer(first_run: bool) -> None:
    if not frappe.db.exists("Tax Category", "Estero"):
        # without a 0% template to resolve, a foreign customer would silently be
        # billed at the standard rate: leave the customer alone and say so
        frappe.msgprint(
            _("Tax Category 'Estero' is missing: {0} was left untouched, set its tax category by hand.").format(
                TRISON_CUSTOMER
            ),
            title=_("MSP field service"),
            indicator="orange",
        )
        return

    current = frappe.db.get_value(
        "Customer", TRISON_CUSTOMER, ["fab_msp_billing_mode", "language", "tax_category"], as_dict=True
    )
    targets = {"fab_msp_billing_mode": "Consolidated", "tax_category": "Estero"}
    if frappe.db.exists("Language", "es"):
        targets["language"] = "es"
    values = {f: v for f, v in targets.items() if first_run or not current.get(f)}
    if values:
        frappe.db.set_value("Customer", TRISON_CUSTOMER, values)


def get_custom_fields() -> dict:
    """The field service fields, merged into install.get_custom_fields()."""
    return {
        # one Task is one on-site intervention: the Trison parte de ticket
        "Task": [
            {
                "fieldname": "fab_fs_section",
                "label": "Field Service",
                "fieldtype": "Section Break",
                "insert_after": "priority",
            },
            {
                "fieldname": "fab_fs_parte",
                "label": "Parte",
                "fieldtype": "Data",
                "insert_after": "fab_fs_section",
                "in_list_view": 1,
                "in_standard_filter": 1,
                "description": "The Trison parte number, e.g. A26/015047. Setting it makes the task an intervention.",
            },
            {
                "fieldname": "fab_fs_ticket_id",
                "label": "Trison Ticket ID",
                "fieldtype": "Data",
                "insert_after": "fab_fs_parte",
                "depends_on": "fab_fs_parte",
            },
            {
                "fieldname": "fab_fs_intervention_date",
                "label": "Intervention Date",
                "fieldtype": "Date",
                "insert_after": "fab_fs_ticket_id",
                "depends_on": "fab_fs_parte",
                "in_list_view": 1,
            },
            {
                "fieldname": "fab_fs_col_head",
                "fieldtype": "Column Break",
                "insert_after": "fab_fs_intervention_date",
            },
            {
                "fieldname": "fab_fs_end_customer",
                "label": "End Customer",
                "fieldtype": "Data",
                "insert_after": "fab_fs_col_head",
                "depends_on": "fab_fs_parte",
                "description": "The Trison delegacion: the shop or showroom we go to.",
            },
            {
                "fieldname": "fab_fs_end_customer_address",
                "label": "End Customer Address",
                "fieldtype": "Small Text",
                "insert_after": "fab_fs_end_customer",
                "depends_on": "fab_fs_parte",
            },
            {
                "fieldname": "fab_fs_report_original",
                "label": "Trison Report",
                "fieldtype": "Attach",
                "insert_after": "fab_fs_end_customer_address",
                "depends_on": "fab_fs_parte",
                "description": "The pre-filled parte received from Trison. Attachments on a task are kept private.",
            },
            {
                "fieldname": "fab_fs_work_section",
                "label": "Work",
                "fieldtype": "Section Break",
                "insert_after": "fab_fs_report_original",
                "depends_on": "fab_fs_parte",
            },
            {
                "fieldname": "fab_fs_ticket_description",
                "label": "Ticket Description",
                "fieldtype": "Text",
                "insert_after": "fab_fs_work_section",
                "description": "The fault as Trison described it.",
            },
            {
                "fieldname": "fab_fs_col_work",
                "fieldtype": "Column Break",
                "insert_after": "fab_fs_ticket_description",
            },
            {
                "fieldname": "fab_fs_work_performed",
                "label": "Work Performed",
                "fieldtype": "Text",
                "insert_after": "fab_fs_col_work",
            },
            {
                "fieldname": "fab_fs_technician_section",
                "label": "Technician",
                "fieldtype": "Section Break",
                "insert_after": "fab_fs_work_performed",
                "depends_on": "fab_fs_parte",
            },
            {
                "fieldname": "fab_fs_technician_kind",
                "label": "Technician Kind",
                "fieldtype": "Select",
                "options": "Internal\nExternal",
                "default": "Internal",
                "insert_after": "fab_fs_technician_section",
            },
            {
                "fieldname": "fab_fs_employee",
                "label": "Employee",
                "fieldtype": "Link",
                "options": "Employee",
                "depends_on": "eval:doc.fab_fs_technician_kind=='Internal'",
                "insert_after": "fab_fs_technician_kind",
            },
            {
                "fieldname": "fab_fs_supplier",
                "label": "Supplier",
                "fieldtype": "Link",
                "options": "Supplier",
                "depends_on": "eval:doc.fab_fs_technician_kind=='External'",
                "insert_after": "fab_fs_employee",
                "description": "The external technician. A technician user is restricted to their own supplier.",
            },
            {
                "fieldname": "fab_fs_technician_user",
                "label": "Technician User",
                "fieldtype": "Link",
                "options": "User",
                "insert_after": "fab_fs_supplier",
            },
            {
                "fieldname": "fab_fs_col_times",
                "fieldtype": "Column Break",
                "insert_after": "fab_fs_technician_user",
            },
            {
                "fieldname": "fab_fs_arrival_time",
                "label": "Arrival Time",
                "fieldtype": "Time",
                "insert_after": "fab_fs_col_times",
            },
            {
                "fieldname": "fab_fs_departure_time",
                "label": "Departure Time",
                "fieldtype": "Time",
                "insert_after": "fab_fs_arrival_time",
                "description": "Earlier than the arrival means the job ran past midnight.",
            },
            {
                "fieldname": "fab_fs_travel_time",
                "label": "Travel Time (h)",
                "fieldtype": "Float",
                "insert_after": "fab_fs_departure_time",
            },
            {
                "fieldname": "fab_fs_hours_billed",
                "label": "Hours Billed",
                "fieldtype": "Float",
                "insert_after": "fab_fs_travel_time",
                "description": "Defaults to the time on site rounded up to the half hour.",
            },
            {
                "fieldname": "fab_fs_material_section",
                "label": "Material",
                "fieldtype": "Section Break",
                "insert_after": "fab_fs_hours_billed",
                "depends_on": "fab_fs_parte",
                "collapsible": 1,
            },
            {
                "fieldname": "fab_fs_materials_used",
                "label": "Material Used",
                "fieldtype": "Table",
                "options": "MSP Field Material",
                "insert_after": "fab_fs_material_section",
            },
            {
                "fieldname": "fab_fs_materials_removed",
                "label": "Material Removed",
                "fieldtype": "Table",
                "options": "MSP Field Material",
                "insert_after": "fab_fs_materials_used",
            },
            {
                "fieldname": "fab_fs_travel",
                "label": "Travel",
                "fieldtype": "Table",
                "options": "MSP Field Travel",
                "insert_after": "fab_fs_materials_removed",
            },
            *esignature.task_custom_fields(),
            {
                "fieldname": "fab_fs_billing_section",
                "label": "Field Service Billing",
                "fieldtype": "Section Break",
                "insert_after": "fab_fs_signature_audit",
                "depends_on": "fab_fs_parte",
                "permlevel": 1,
            },
            {
                "fieldname": "fab_fs_callout_fee",
                "label": "Call-out Fee",
                "fieldtype": "Currency",
                "default": str(DEFAULT_CALLOUT_FEE),
                "insert_after": "fab_fs_billing_section",
                "permlevel": 1,
            },
            {
                "fieldname": "fab_fs_hourly_rate",
                "label": "Hourly Rate",
                "fieldtype": "Currency",
                "default": str(DEFAULT_HOURLY_RATE),
                "insert_after": "fab_fs_callout_fee",
                "permlevel": 1,
            },
            {
                "fieldname": "fab_fs_billing_status",
                "label": "Billing Status",
                "fieldtype": "Select",
                "options": "Not billable\nTo bill\nBilled",
                "default": "Not billable",
                "read_only": 1,
                "insert_after": "fab_fs_hourly_rate",
                "permlevel": 1,
            },
            {
                "fieldname": "fab_fs_col_billing",
                "fieldtype": "Column Break",
                "insert_after": "fab_fs_billing_status",
                "permlevel": 1,
            },
            {
                "fieldname": "fab_fs_callout_charge",
                "label": "Call-out Charge",
                "fieldtype": "Link",
                "options": "MSP Billing Charge",
                "read_only": 1,
                "insert_after": "fab_fs_col_billing",
                "permlevel": 1,
            },
            {
                "fieldname": "fab_fs_labour_charge",
                "label": "Labour Charge",
                "fieldtype": "Link",
                "options": "MSP Billing Charge",
                "read_only": 1,
                "insert_after": "fab_fs_callout_charge",
                "permlevel": 1,
            },
            {
                "fieldname": "fab_fs_sales_invoice",
                "label": "Sales Invoice",
                "fieldtype": "Link",
                "options": "Sales Invoice",
                "read_only": 1,
                "insert_after": "fab_fs_labour_charge",
                "permlevel": 1,
            },
            {
                "fieldname": "fab_fs_purchase_invoice",
                "label": "Technician Invoice",
                "fieldtype": "Link",
                "options": "Purchase Invoice",
                "insert_after": "fab_fs_sales_invoice",
                "permlevel": 1,
                "description": "The external technician's invoice, paid once Trison has paid.",
            },
        ],
    }
