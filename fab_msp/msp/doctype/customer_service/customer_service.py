import frappe
from frappe.model.document import Document


class CustomerService(Document):
    def validate(self):
        self.inherit_billing_defaults()

    def inherit_billing_defaults(self):
        """Fill billing item and mode from the service type when left blank."""
        if not self.service_type:
            return
        if self.billing_item and self.billing_mode and self.billing_mode != "None":
            return
        service_type = frappe.get_cached_doc("Customer Service Type", self.service_type)
        if not self.billing_item and service_type.default_billing_item:
            self.billing_item = service_type.default_billing_item
        if (not self.billing_mode or self.billing_mode == "None") and service_type.default_billing_mode:
            self.billing_mode = service_type.default_billing_mode
        if not self.billing_interval and service_type.get("default_billing_interval"):
            self.billing_interval = service_type.default_billing_interval
