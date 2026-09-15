frappe.ui.form.on("Sales Invoice", {
	refresh(frm) {
		if (frm.doc.docstatus !== 1) {
			return;
		}

		if (frm.doc.custom_billingo_document_id) {
			frm.add_custom_button(__("Send via Billingo"), () => {
				frappe.confirm(
					__("Send this Billingo invoice to the Customer's primary Contact email now?"),
					() => {
						frappe.call({
							method: "billingo.api.send_billingo_invoice",
							args: { sales_invoice: frm.doc.name },
							freeze: true,
							freeze_message: __("Sending via Billingo..."),
							callback(response) {
								const email = response.message && response.message.email;
								frappe.show_alert({
									message: __("Billingo send request completed for {0}", [email]),
									indicator: "green",
								});
							},
						});
					}
				);
			});
		}

		if (frm.doc.custom_billingo_sync_status !== "Failed") {
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
