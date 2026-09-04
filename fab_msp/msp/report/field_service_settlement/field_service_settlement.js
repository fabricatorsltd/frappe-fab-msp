frappe.query_reports["Field Service Settlement"] = {
	filters: [
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
		},
		{
			fieldname: "payable_only",
			label: __("Payable now only"),
			fieldtype: "Check",
		},
	],
};
