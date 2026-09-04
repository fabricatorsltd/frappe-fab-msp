app_name = "fab_msp"
app_title = "Fab MSP"
app_publisher = "fabricators"
app_description = "Managed services: customer service CMDB, request catalog, approvals and ERP billing glue"
app_email = "support@fabricators.ltd"
app_license = "agpl-3.0"

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "fab_msp",
# 		"logo": "/assets/fab_msp/logo.png",
# 		"title": "Fab MSP",
# 		"route": "/fab_msp",
# 		"has_permission": "fab_msp.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/fab_msp/css/fab_msp.css"
# app_include_js = "/assets/fab_msp/js/fab_msp.js"

# include js, css files in header of web template
# web_include_css = "/assets/fab_msp/css/fab_msp.css"
# web_include_js = "/assets/fab_msp/js/fab_msp.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "fab_msp/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
doctype_js = {"Task": "public/js/task_field_service.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "fab_msp/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# automatically load and sync documents of this doctype from downstream apps
# importable_doctypes = [doctype_1]

# Jinja
# ----------

# add methods and filters to jinja environment
jinja = {
	"methods": ["fab_msp.field_service.trison_parte"],
}

# Installation
# ------------

# before_install = "fab_msp.install.before_install"
after_install = "fab_msp.install.after_install"
after_migrate = "fab_msp.install.after_migrate"

# Uninstallation
# ------------

# before_uninstall = "fab_msp.uninstall.before_uninstall"
# after_uninstall = "fab_msp.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "fab_msp.utils.before_app_install"
# after_app_install = "fab_msp.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "fab_msp.utils.before_app_uninstall"
# after_app_uninstall = "fab_msp.utils.after_app_uninstall"

# Build
# ------------------
# To hook into the build process

# after_build = "fab_msp.build.after_build"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "fab_msp.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# Document Events
# ---------------
# Hook on document methods and events

doc_events = {
	"HD Ticket": {
		"validate": "fab_msp.ticket.apply_service_rules",
		"on_update": "fab_msp.ticket.maybe_fulfill_on_close",
	},
	"Task": {
		"validate": "fab_msp.field_service.apply_field_service_defaults",
		"on_update": "fab_msp.field_service.maybe_bill_on_close",
	},
	"Sales Invoice": {
		"on_submit": "fab_msp.billing.reflect_invoice_on_submit",
		"on_cancel": "fab_msp.billing.release_invoice_charges",
		"on_trash": "fab_msp.billing.release_invoice_charges",
	},
	"File": {
		"after_insert": "fab_msp.field_service.force_private_attachment",
	},
}

permission_query_conditions = {
	"Task": "fab_msp.field_service.task_query_conditions",
}

has_permission = {
	"Task": "fab_msp.field_service.task_has_permission",
}

# Scheduled Tasks
# ---------------

scheduler_events = {
	"hourly": [
		"fab_msp.esignature.poll_pending_signatures",
	],
}

# scheduler_events = {
# 	"all": [
# 		"fab_msp.tasks.all"
# 	],
# 	"daily": [
# 		"fab_msp.tasks.daily"
# 	],
# 	"hourly": [
# 		"fab_msp.tasks.hourly"
# 	],
# 	"weekly": [
# 		"fab_msp.tasks.weekly"
# 	],
# 	"monthly": [
# 		"fab_msp.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "fab_msp.install.before_tests"

# Extend DocType Class
# ------------------------------
#
# Specify custom mixins to extend the standard doctype controller.
# extend_doctype_class = {
# 	"Task": "fab_msp.custom.task.CustomTaskMixin"
# }

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "fab_msp.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "fab_msp.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["fab_msp.utils.before_request"]
# after_request = ["fab_msp.utils.after_request"]

# Job Events
# ----------
# before_job = ["fab_msp.utils.before_job"]
# after_job = ["fab_msp.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"fab_msp.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []

