frappe.ui.form.on("MSP Consolidated Billing", {
	generate(frm) {
		frappe.call({
			method: "fab_msp.billing.generate_consolidated_invoices",
			args: { posting_date: frm.doc.posting_date },
			freeze: true,
			freeze_message: __("Generating consolidated invoices..."),
			callback: (r) => {
				const created = r.message || [];
				const summary = created.length
					? __("{0} draft invoice(s) created", [created.length]) + ": " + created.join(", ")
					: __("Nothing to bill for this period.");
				frm.set_value("last_run", summary);
				if (created.length) {
					const links = created
						.map((n) => `<a href="/app/sales-invoice/${encodeURIComponent(n)}">${n}</a>`)
						.join("<br>");
					frappe.msgprint({ title: __("Consolidated invoices"), message: links, indicator: "green" });
				} else {
					frappe.show_alert(__("Nothing to bill for this period."));
				}
			},
		});
	},
});
