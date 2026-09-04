// The signature of the Trison parte, from the technician's own task form.
frappe.ui.form.on("Task", {
	refresh(frm) {
		if (frm.is_new() || !frm.doc.fab_fs_parte) return;
		const status = frm.doc.fab_fs_signature_status || "Not sent";
		const sendable = ["Not sent", "Error", "Expired", "Declined"].includes(status);

		if (frm.doc.status === "Completed" && sendable) {
			frm.add_custom_button(__("Send for signature"), () => send_for_signature(frm));
		}
		if (["Sent", "Partially signed"].includes(status)) {
			frm.add_custom_button(__("Refresh signature status"), () => refresh_signature(frm));
			frm.add_custom_button(__("Signing links"), () => signing_links(frm));
		}
	},
});

function signing_links(frm) {
	frappe.call({
		method: "fab_msp.esignature.get_signing_links",
		args: { task: frm.doc.name },
		callback: (r) => show_links(frm, (r.message || {}).links || {}, (r.message || {}).qr || {}),
	});
}

function send_for_signature(frm) {
	frappe.call({
		method: "fab_msp.esignature.send_for_signature",
		args: { task: frm.doc.name },
		freeze: true,
		freeze_message: __("Sending the parte for signature..."),
		callback: (r) => {
			if (!r.message) return;
			frm.reload_doc();
			show_links(frm, r.message.links || {}, r.message.qr || {});
		},
	});
}

function refresh_signature(frm) {
	frappe.call({
		method: "fab_msp.esignature.refresh_signature",
		args: { task: frm.doc.name },
		freeze: true,
		callback: (r) => {
			frm.reload_doc();
			if (r.message) frappe.show_alert(__("Signature is {0}", [__(r.message)]));
		},
	});
}

// the manager signs on their own phone, so their link is also a QR to hold out
function show_links(frm, links, qr) {
	const labels = { technician: __("Technician"), manager: __("Shop manager") };
	const blocks = ["manager", "technician"]
		.filter((role) => links[role])
		.map((role) => {
			const url = frappe.utils.escape_html(links[role]);
			const image = qr[role]
				? `<div><img src="${frappe.utils.escape_html(qr[role])}" alt="${labels[role]}"></div>`
				: "";
			return `<p><b>${labels[role]}</b><br>
				<a href="${url}" target="_blank">${url}</a></p>${image}`;
		});
	if (!blocks.length) {
		frappe.msgprint(__("No signing link was stored on this parte."));
		return;
	}
	frappe.msgprint({
		title: __("Sign the parte"),
		message: blocks.join("<hr>"),
		wide: true,
	});
}
