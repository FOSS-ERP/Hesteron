"""
Billingo <-> ERPNext integration.

Flow:
  Sales Invoice Draft  (after_insert / on_update, docstatus 0)
      -> push a Billingo `type: "draft"` document.
      -> first save: POST /documents
      -> every subsequent save while still Draft: DELETE the old
         draft + POST a new one (Billingo has no endpoint to edit a
         draft's contents in place - confirmed against the v3
         OpenAPI spec: PUT /documents/{id} always *converts* a draft
         to an invoice, it never just saves an edit).

  Sales Invoice Submit (on_submit, docstatus 1)
      -> PUT /documents/{id} on the SAME billingo id, with
         type: "invoice". This converts the draft into a real,
         numbered invoice in place - no new Billingo document id.
"""

import frappe
import requests

BILLINGO_BASE_URL = "https://api.billingo.hu/v3"

DEFAULT_PAYMENT_METHOD = "wire_transfer"

MODE_OF_PAYMENT_MAP = {
    "Cash": "cash",
    "Wire Transfer": "wire_transfer",
    "Bank Draft": "wire_transfer",
    "Credit Card": "bankcard",
    "Cheque": "postai_csekk",
    "Debit Card": "bankcard",
    "Direct Debit": "wire_transfer",
    "Phone": "online_bankcard",
}


def _get_headers():
    api_key = frappe.conf.get("billingo_api_key")
    if not api_key:
        frappe.throw("billingo_api_key is not set in site_config.json")
    return {
        "X-API-KEY": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _get_error_text(response):
    try:
        data = response.json()
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict) and error.get("message"):
                return str(error["message"])
            if data.get("message"):
                return str(data["message"])
    except Exception:
        pass
    return response.text or "Unknown Billingo error"


# ----------------------------------------------------------------------
# Partner / customer (unchanged from the working version)
# ----------------------------------------------------------------------

def _get_or_create_partner(customer_name):
    customer = frappe.get_doc("Customer", customer_name)

    existing_id = customer.get("custom_billingo_partner_id")
    if existing_id:
        return existing_id

    address_line, city, postal_code, country_code = _get_customer_address(customer_name)
    email = _get_customer_email(customer_name)

    payload = {
        "name": customer.customer_name,
        "address": {
            "country_code": country_code or "HU",
            "post_code": postal_code or "",
            "city": city or "",
            "address": address_line or "",
        },
        "emails": [email] if email else [],
        "taxcode": customer.get("tax_id") or "",
    }

    response = requests.post(
        f"{BILLINGO_BASE_URL}/partners",
        json=payload,
        headers=_get_headers(),
        timeout=15,
    )
    response.raise_for_status()
    partner_id = response.json()["id"]

    frappe.db.set_value("Customer", customer_name, "custom_billingo_partner_id", partner_id)
    frappe.db.commit()
    return partner_id


def _get_customer_address(customer_name):
    address_name = frappe.db.get_value(
        "Dynamic Link",
        {"link_doctype": "Customer", "link_name": customer_name, "parenttype": "Address"},
        "parent",
    )
    if not address_name:
        return "", "", "", ""

    address = frappe.get_doc("Address", address_name)
    address_line = " ".join(filter(None, [address.address_line1, address.address_line2]))
    country_code = frappe.db.get_value("Country", address.country, "code") if address.country else ""
    return address_line, address.city or "", address.pincode or "", (country_code or "").upper()


def _get_customer_email(customer_name):
    email = frappe.db.get_value(
        "Contact",
        {"links.link_doctype": "Customer", "links.link_name": customer_name},
        "email_id",
    )
    return email or ""


# ----------------------------------------------------------------------
# Items / VAT / payment method / comment (unchanged from working version)
# ----------------------------------------------------------------------

def _get_flat_charge_rows(sales_invoice):
    return [
        tax_row
        for tax_row in (sales_invoice.get("taxes") or [])
        if tax_row.charge_type == "Actual" and tax_row.tax_amount
    ]


def _map_extra_charges(sales_invoice):
    extra_items = []
    for tax_row in _get_flat_charge_rows(sales_invoice):
        extra_items.append({
            "name": tax_row.description or tax_row.account_head or "Additional charge",
            "unit_price": tax_row.tax_amount,
            "unit_price_type": "net",
            "quantity": 1,
            "unit": "pcs",
            "vat": "0%",
            "comment": "Auto-added from Sales Invoice taxes table (flat charge)",
        })
    return extra_items


def _map_vat_rate(sales_invoice):
    percentage_charge_types = {"On Net Total", "On Previous Row Total", "On Previous Row Amount"}
    for tax_row in sales_invoice.get("taxes") or []:
        if tax_row.charge_type in percentage_charge_types and tax_row.rate:
            return f"{int(tax_row.rate)}%"
    return "0%"


def _map_items(sales_invoice):
    vat_rate = _map_vat_rate(sales_invoice)
    items = []
    for row in sales_invoice.items:
        items.append({
            "name": row.item_name or row.item_code,
            "unit_price": row.rate,
            "unit_price_type": "net",
            "quantity": row.qty,
            "unit": row.uom or "pcs",
            "vat": vat_rate,
            "comment": row.description or "",
        })
    items.extend(_map_extra_charges(sales_invoice))
    return items


def _get_mode_of_payment(doc):
    for row in doc.get("payment_schedule") or []:
        term_name = row.get("payment_term")
        if not term_name:
            continue
        mode_of_payment = frappe.db.get_value("Payment Term", term_name, "mode_of_payment")
        if mode_of_payment:
            return mode_of_payment
    return None


def _map_payment_method(doc):
    mode_of_payment = _get_mode_of_payment(doc)
    return MODE_OF_PAYMENT_MAP.get(mode_of_payment, DEFAULT_PAYMENT_METHOD)


def _build_payment_term_lines(doc):
    rows = doc.get("payment_schedule") or []
    lines = []
    for row in rows:
        term_name = row.get("payment_term")
        description = row.get("description")
        mode_of_payment = None

        if term_name:
            term_fields = frappe.db.get_value(
                "Payment Term", term_name, ["description", "mode_of_payment"], as_dict=True
            )
            if term_fields:
                description = description or term_fields.get("description")
                mode_of_payment = term_fields.get("mode_of_payment")

        if not description and not term_name:
            continue

        detail = description or term_name
        extras = []
        if row.get("invoice_portion"):
            extras.append(f"{row.invoice_portion}% due")
        if row.get("due_date"):
            extras.append(f"by {row.due_date}")

        line = f"Payment Term: {detail}"
        if extras:
            line += f" ({', '.join(extras)})"
        lines.append(line)

        if mode_of_payment:
            lines.append(f"Mode of Payment: {mode_of_payment}")

    return lines


def _build_comment(doc):
    lines = [f"ERPNext Sales Invoice {doc.name}"]
    lines.extend(_build_payment_term_lines(doc))

    if doc.get("shipping_rule"):
        lines.append(f"Shipping Rule: {doc.shipping_rule}")

    if doc.get("incoterm"):
        incoterm_line = f"Incoterm: {doc.incoterm}"
        if doc.get("named_place"):
            incoterm_line += f" ({doc.named_place})"
        lines.append(incoterm_line)

    if doc.get("tc_name") or doc.get("terms"):
        lines.append("Terms and Conditions")

    if doc.get("tc_name"):
        lines.append(doc.tc_name)

    if doc.get("terms"):
        plain_terms = frappe.utils.strip_html(doc.terms).strip()
        if plain_terms:
            snippet = plain_terms[:300]
            if len(plain_terms) > 300:
                snippet += "..."
            lines.append(snippet)

    return "\n".join(lines)


def _build_billingo_payload(doc, partner_id, doc_type):
    return {
        "partner_id": partner_id,
        "block_id": 0,
        "type": doc_type,          # "draft" while ERPNext is Draft, "invoice" on submit
        "fulfillment_date": str(doc.posting_date),
        "due_date": str(doc.due_date or doc.posting_date),
        "payment_method": _map_payment_method(doc),
        "language": "en",
        "currency": doc.currency or "EUR",
        "conversion_rate": doc.conversion_rate or 1,
        "electronic": False,
        "paid": False,
        "items": _map_items(doc),
        "comment": _build_comment(doc),
    }


# ----------------------------------------------------------------------
# Draft push (ERPNext Draft -> Billingo draft)
# ----------------------------------------------------------------------

def sync_billingo_draft(doc, method=None):
    """
    doc_events hook: Sales Invoice after_insert + on_update.
    Only acts while the ERPNext invoice is still a Draft (docstatus 0).
    """
    if doc.docstatus != 0:
        return
    if not doc.customer:
        return

    try:
        partner_id = _get_or_create_partner(doc.customer)
        payload = _build_billingo_payload(doc, partner_id, "draft")

        existing_id = doc.get("custom_billingo_document_id")

        # If a draft already exists in Billingo, it has to be deleted
        # and recreated - Billingo has no endpoint to edit a draft's
        # contents in place.
        if existing_id:
            delete_response = requests.delete(
                f"{BILLINGO_BASE_URL}/documents/{existing_id}",
                headers=_get_headers(),
                timeout=15,
            )
            # 404 just means it's already gone (e.g. manually removed
            # in Billingo) - fine to proceed and create a fresh one.
            if delete_response.status_code not in (204, 404):
                delete_response.raise_for_status()

        response = requests.post(
            f"{BILLINGO_BASE_URL}/documents",
            json=payload,
            headers=_get_headers(),
            timeout=20,
        )
        response.raise_for_status()
        result = response.json()

        doc.db_set("custom_billingo_document_id", result.get("id"))
        doc.db_set("custom_billingo_sync_status", "Synced")
        doc.db_set("custom_billingo_error", "")

    except requests.exceptions.HTTPError as e:
        error_detail = _get_error_text(e.response) if e.response is not None else str(e)
        doc.db_set("custom_billingo_sync_status", "Failed")
        doc.db_set("custom_billingo_error", error_detail[:500])
        frappe.log_error(error_detail, "Billingo Draft Sync Error")

    except Exception as e:
        doc.db_set("custom_billingo_sync_status", "Failed")
        doc.db_set("custom_billingo_error", str(e)[:500])
        frappe.log_error(frappe.get_traceback(), "Billingo Draft Sync Error")


# ----------------------------------------------------------------------
# Submit (Billingo draft -> Billingo invoice, SAME id)
# ----------------------------------------------------------------------

def finalize_billingo_invoice(doc, method=None):
    """
    doc_events hook: Sales Invoice on_submit.
    Converts the existing Billingo draft into a real invoice in place.
    """
    billingo_id = doc.get("custom_billingo_document_id")

    if not billingo_id:
        # No draft was ever pushed (e.g. customer was blank while in
        # Draft) - fall back to creating a fresh invoice directly.
        try:
            partner_id = _get_or_create_partner(doc.customer)
            payload = _build_billingo_payload(doc, partner_id, "invoice")

            response = requests.post(
                f"{BILLINGO_BASE_URL}/documents",
                json=payload,
                headers=_get_headers(),
                timeout=20,
            )
            response.raise_for_status()
            result = response.json()

            doc.db_set("custom_billingo_document_id", result.get("id"))
            doc.db_set("custom_billingo_invoice_number", result.get("invoice_number"))
            doc.db_set("custom_billingo_sync_status", "Invoiced")
            doc.db_set("custom_billingo_error", "")
            return

        except requests.exceptions.HTTPError as e:
            error_detail = _get_error_text(e.response) if e.response is not None else str(e)
            doc.db_set("custom_billingo_sync_status", "Failed")
            doc.db_set("custom_billingo_error", error_detail[:500])
            frappe.log_error(error_detail, "Billingo Finalization Error")
            return

        except Exception as e:
            doc.db_set("custom_billingo_sync_status", "Failed")
            doc.db_set("custom_billingo_error", str(e)[:500])
            frappe.log_error(frappe.get_traceback(), "Billingo Finalization Error")
            return

    try:
        partner_id = _get_or_create_partner(doc.customer)
        payload = _build_billingo_payload(doc, partner_id, "invoice")

        # PUT /documents/{id} converts the draft into an invoice -
        # same Billingo id, now finalized and numbered.
        response = requests.put(
            f"{BILLINGO_BASE_URL}/documents/{billingo_id}",
            json=payload,
            headers=_get_headers(),
            timeout=20,
        )
        response.raise_for_status()
        result = response.json()

        doc.db_set("custom_billingo_invoice_number", result.get("invoice_number"))
        doc.db_set("custom_billingo_sync_status", "Invoiced")
        doc.db_set("custom_billingo_error", "")

    except requests.exceptions.HTTPError as e:
        error_detail = _get_error_text(e.response) if e.response is not None else str(e)
        doc.db_set("custom_billingo_sync_status", "Failed")
        doc.db_set("custom_billingo_error", error_detail[:500])
        frappe.log_error(error_detail, "Billingo Finalization Error")

    except Exception as e:
        doc.db_set("custom_billingo_sync_status", "Failed")
        doc.db_set("custom_billingo_error", str(e)[:500])
        frappe.log_error(frappe.get_traceback(), "Billingo Finalization Error")
