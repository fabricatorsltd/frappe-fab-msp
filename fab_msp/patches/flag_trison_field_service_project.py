import frappe

from fab_msp import field_service
from fab_msp.install import ensure_custom_fields


def execute():
    """Mark the existing Trison project as field service and copy the flag onto
    its tasks, which only fetch it on their next save."""
    ensure_custom_fields()
    project = frappe.db.get_value("Project", {"project_name": field_service.TRISON_PROJECT}, "name")
    if not project:
        return
    frappe.db.set_value("Project", project, "fab_fs_field_service", 1, update_modified=False)
    frappe.db.set_value(
        "Task", {"project": project}, "fab_fs_project_field_service", 1, update_modified=False
    )
