frappe.ui.form.on("Sales Invoice", {
	refresh(frm) {
		if (frm.doc.docstatus !== 1 || frm.doc.custom_billingo_sync_status !== "Failed") {
			return;
		}

		frm.add_custom_button(__("Retry Billingo Sync"), () => {
			frappe.confirm(
				__(
					"Retry Billingo for this submitted invoice? Use this only after confirming " +
						"that Billingo does not already contain the invoice."
				),
				() => {
					frappe.call({
						method: "billingo.api.retry_billingo_sync",
						args: { sales_invoice: frm.doc.name },
						freeze: true,
						freeze_message: __("Retrying Billingo sync..."),
						callback() {
							frm.reload_doc();
						},
					});
				}
			);
		});
	},
});
