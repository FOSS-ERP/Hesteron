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

import json

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

# Billingo bank_account_id values are specific to the Hesteron Kft. live
# Billingo account (Profil azonosito: 190-191) - do not reuse against the
# test/sandbox Billingo account, its ids differ. HUF (and any unmapped
# currency) is intentionally left unset, so Billingo's own account-level
# default bank account applies (currently BinX HUF, id 284393).
BANK_ACCOUNT_ID_BY_CURRENCY = {
    "EUR": 280724,  # Billingo bank account: IbanFirst EUR
    "USD": 294227,  # Billingo bank account: IbanFirst USD
}

# This is deliberately a site setting, rather than a source-code constant:
# document blocks belong to a Billingo profile, just like partners and bank
# accounts.  Set it with:
#   bench --site <site> set-config billingo_document_block_id <block-id>
DOCUMENT_BLOCK_CONFIG_KEY = "billingo_document_block_id"


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
                # Billingo puts useful validation information in sibling
                # fields (notably ``errors``).  Preserve the complete API
                # response instead of reducing it to "Validation Failed".
                return json.dumps(data, ensure_ascii=False, default=str)
            if data.get("message"):
                return json.dumps(data, ensure_ascii=False, default=str)
            return json.dumps(data, ensure_ascii=False, default=str)
    except Exception:
        pass
    return response.text or "Unknown Billingo error"


# ----------------------------------------------------------------------
# Partner / customer
# ----------------------------------------------------------------------

def _get_document_block_id():
    """Return the Billingo invoice block configured for this Frappe site."""
    block_id = frappe.conf.get(DOCUMENT_BLOCK_CONFIG_KEY)
    if block_id in (None, ""):
        frappe.throw(
            f"{DOCUMENT_BLOCK_CONFIG_KEY} is not set in site_config.json. "
            "Configure a valid Billingo invoice document-block ID before syncing."
        )

    try:
        block_id = int(block_id)
    except (TypeError, ValueError):
        frappe.throw(f"{DOCUMENT_BLOCK_CONFIG_KEY} must be a positive integer.")

    if block_id <= 0:
        frappe.throw(f"{DOCUMENT_BLOCK_CONFIG_KEY} must be a positive integer.")
    return block_id


def _get_document_by_vendor_id(vendor_id):
    """Return a previously created Billingo document for an ERPNext reference."""
    response = requests.get(
        f"{BILLINGO_BASE_URL}/documents/vendor/{vendor_id}",
        headers=_get_headers(),
        timeout=15,
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def _clear_partner_id(customer_name):
    frappe.db.set_value("Customer", customer_name, "custom_billingo_partner_id", None)


def _stored_partner_is_accessible(partner_id):
    """Check the stored profile-scoped partner ID against the current key."""
    response = requests.get(
        f"{BILLINGO_BASE_URL}/partners/{partner_id}",
        headers=_get_headers(),
        timeout=15,
    )
    if response.ok:
        return True
    if response.status_code in (403, 404):
        return False
    response.raise_for_status()
    return False


def _find_existing_partner(customer):
    """Reuse an exact-name partner in the active Billingo profile if present."""
    response = requests.get(
        f"{BILLINGO_BASE_URL}/partners",
        params={"query": customer.customer_name, "per_page": 100},
        headers=_get_headers(),
        timeout=15,
    )
    response.raise_for_status()

    for partner in response.json().get("data", []):
        if (partner.get("name") or "").strip().casefold() == customer.customer_name.strip().casefold():
            return partner.get("id")
    return None


def _get_required_customer_address(customer_name):
    address_line, city, postal_code, country_code = _get_customer_address(customer_name)
    values = {
        "street address": address_line,
        "city": city,
        "postal code": postal_code,
    }
    missing = [label for label, value in values.items() if not value]
    if missing:
        frappe.throw(
            f"Cannot create a Billingo partner for Customer '{customer_name}': "
            f"missing {', '.join(missing)} on its linked Address."
        )
    return address_line, city, postal_code, country_code or "HU"

def _get_or_create_partner(customer_name):
    customer = frappe.get_doc("Customer", customer_name)

    existing_id = customer.get("custom_billingo_partner_id")
    if existing_id:
        if _stored_partner_is_accessible(existing_id):
            return existing_id

        # IDs are only valid in the Billingo profile that created them.  A
        # changed API key can therefore make an otherwise valid Customer
        # record unusable.  Clear it and recover under the active profile.
        _clear_partner_id(customer_name)

    partner_id = _find_existing_partner(customer)
    if partner_id:
        frappe.db.set_value("Customer", customer_name, "custom_billingo_partner_id", partner_id)
        return partner_id

    address_line, city, postal_code, country_code = _get_required_customer_address(customer_name)
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
    is_hungarian = _get_invoice_language(sales_invoice) == "hu"
    comment = (
        "Automatikusan hozzáadva a Sales Invoice adók táblából (tételes díj)"
        if is_hungarian
        else "Auto-added from Sales Invoice taxes table (flat charge)"
    )
    extra_items = []
    for tax_row in _get_flat_charge_rows(sales_invoice):
        extra_items.append({
            "name": tax_row.description or tax_row.account_head or "Additional charge",
            "unit_price": tax_row.tax_amount,
            "unit_price_type": "net",
            "quantity": 1,
            "unit": "pcs",
            "vat": "0%",
            "comment": comment,
        })
    return extra_items


def _map_vat_rate(sales_invoice):
    percentage_charge_types = {"On Net Total", "On Previous Row Total", "On Previous Row Amount"}
    for tax_row in sales_invoice.get("taxes") or []:
        if tax_row.charge_type in percentage_charge_types and tax_row.rate:
            return f"{int(tax_row.rate)}%"
    return "0%"


def _format_item_name(row):
    """
    "ITEMCODE - Item Name" for the Billingo line-item title. Falls back to
    whichever of code/name is present, and avoids "CODE - CODE" when there's
    no distinct item_name.
    """
    item_code = row.item_code
    item_name = row.item_name or item_code
    if item_code and item_code != item_name:
        return f"{item_code} \u2013 {item_name}"
    return item_name or item_code


def _get_serial_numbers(row):
    """
    Serial numbers for one Sales Invoice Item row. ERPNext v15+ has two ways
    these can be stored, depending on the Item's "Use Serial No / Batch
    Fields" setting:
      - legacy / use_serial_batch_fields checked: newline-separated text
        directly in row.serial_no
      - current default: a linked "Serial and Batch Bundle" document, whose
        child rows (doctype "Serial and Batch Entry") each carry one
        serial_no for this line
    Check both so this works regardless of which mode Balazs's items use.
    """
    if row.get("serial_no"):
        return [s.strip() for s in row.serial_no.split("\n") if s.strip()]

    bundle = row.get("serial_and_batch_bundle")
    if not bundle:
        return []

    return [
        s for s in frappe.get_all(
            "Serial and Batch Entry",
            filters={"parent": bundle},
            pluck="serial_no",
        )
        if s
    ]


def _get_item_comment(row):
    serial_numbers = _get_serial_numbers(row)
    if serial_numbers:
        return "S/N: " + ", ".join(serial_numbers)
    return ""


def _map_items(sales_invoice):
    vat_rate = _map_vat_rate(sales_invoice)
    items = []
    for row in sales_invoice.items:
        items.append({
            "name": _format_item_name(row),
            "unit_price": row.rate,
            "unit_price_type": "net",
            "quantity": row.qty,
            "unit": row.uom or "pcs",
            "vat": vat_rate,
            "comment": _get_item_comment(row),
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


def _build_payment_term_lines(doc, is_hungarian=False):
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
            due_word = "esedékes" if is_hungarian else "due"
            extras.append(f"{row.invoice_portion}% {due_word}")
        if row.get("due_date"):
            by_word = "eddig" if is_hungarian else "by"
            extras.append(f"{by_word} {row.due_date}")

        term_label = "Fizetési feltétel" if is_hungarian else "Payment Term"
        line = f"{term_label}: {detail}"
        if extras:
            line += f" ({', '.join(extras)})"
        lines.append(line)

        if mode_of_payment:
            mode_label = "Fizetési mód" if is_hungarian else "Mode of Payment"
            lines.append(f"{mode_label}: {mode_of_payment}")

    return lines


def _build_comment(doc):
    is_hungarian = _get_invoice_language(doc) == "hu"

    if is_hungarian:
        lines = [f"ERPNext Számla {doc.name}"]
    else:
        lines = [f"ERPNext Sales Invoice {doc.name}"]

    lines.extend(_build_payment_term_lines(doc, is_hungarian))

    if doc.get("shipping_rule"):
        label = "Szállítási mód" if is_hungarian else "Shipping Rule"
        lines.append(f"{label}: {doc.shipping_rule}")

    if doc.get("incoterm"):
        incoterm_line = f"Incoterm: {doc.incoterm}"
        if doc.get("named_place"):
            incoterm_line += f" ({doc.named_place})"
        lines.append(incoterm_line)

    if doc.get("tc_name") or doc.get("terms"):
        lines.append("Általános Szerződési Feltételek" if is_hungarian else "Terms and Conditions")

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


def _get_invoice_language(doc):
    """
    Determines which language Billingo should render the invoice in.
    Driven by the standard ERPNext Customer.language field.
    Default is Hungarian; only an explicit "en" on the Customer
    switches the invoice to English.
    """
    customer_language = frappe.db.get_value("Customer", doc.customer, "language")
    if customer_language == "en":
        return "en"
    return "hu"


def _get_bank_account_id(doc):
    """
    Returns the Billingo bank_account_id to attach for non-HUF invoices.
    HUF (and any other unmapped currency) is left unset, so Billingo's own
    account-level default bank account applies (currently BinX HUF).
    """
    bank_account_id = BANK_ACCOUNT_ID_BY_CURRENCY.get(doc.currency)
    if not bank_account_id and doc.currency and doc.currency != "HUF":
        frappe.log_error(
            f"No Billingo bank_account_id mapped for currency '{doc.currency}' "
            f"on Sales Invoice {doc.name}. Falling back to the Billingo "
            f"account-wide default bank account.",
            "Billingo Bank Account Mapping - Unmapped Currency",
        )
    return bank_account_id


def _build_billingo_payload(doc, partner_id, doc_type):
    payload = {
        # Makes retries idempotent when Billingo created a document but the
        # HTTP response did not reach ERPNext.
        "vendor_id": doc.name,
        "partner_id": partner_id,
        "block_id": _get_document_block_id(),
        "type": doc_type,          # "draft" while ERPNext is Draft, "invoice" on submit
        "fulfillment_date": str(doc.posting_date),
        "due_date": str(doc.due_date or doc.posting_date),
        "payment_method": _map_payment_method(doc),
        "language": _get_invoice_language(doc),
        "currency": doc.currency or "EUR",
        "conversion_rate": doc.conversion_rate or 1,
        "electronic": False,
        "paid": False,
        "items": _map_items(doc),
        "comment": _build_comment(doc),
    }

    bank_account_id = _get_bank_account_id(doc)
    if bank_account_id:
        payload["bank_account_id"] = bank_account_id

    return payload


def _build_modification_payload(doc):
    """Payload accepted by Billingo's linked credit-note endpoint."""
    return {
        "due_date": str(doc.due_date or doc.posting_date),
        "payment_method": _map_payment_method(doc),
        "without_financial_fulfillment": False,
        "items": _map_items(doc),
        "comment": _build_comment(doc),
    }


def _record_draft_error(doc, error_detail):
    doc.db_set("custom_billingo_sync_status", "Failed")
    doc.db_set("custom_billingo_error", error_detail[:500])
    frappe.log_error(error_detail, "Billingo Draft Sync Error")


def _raise_finalization_error(doc, error_detail):
    """Log the remote error and abort the ERPNext submit transaction."""
    frappe.log_error(error_detail, "Billingo Finalization Error")
    frappe.throw(
        f"Billingo finalization failed for Sales Invoice {doc.name}: {error_detail}",
        title="Billingo finalization failed",
    )


def _mark_document_synced(doc, result):
    doc.db_set("custom_billingo_document_id", result.get("id"))
    doc.db_set("custom_billingo_invoice_number", result.get("invoice_number"))
    doc.db_set("custom_billingo_sync_status", "Synced")
    doc.db_set("custom_billingo_error", "")


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
    if doc.is_return:
        # Billingo returns are linked to their submitted originals and cannot
        # be independent negative drafts.
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
        _record_draft_error(doc, error_detail)

    except Exception as e:
        _record_draft_error(doc, str(e))
        frappe.log_error(frappe.get_traceback(), "Billingo Draft Sync Error")


# ----------------------------------------------------------------------
# Submit (Billingo draft -> Billingo invoice, SAME id)
# ----------------------------------------------------------------------

def finalize_billingo_invoice(doc, method=None):
    """
    doc_events hook: Sales Invoice on_submit.
    Converts the existing Billingo draft into a real invoice in place.
    """
    try:
        if doc.is_return:
            original_billingo_id = frappe.db.get_value(
                "Sales Invoice", doc.return_against, "custom_billingo_document_id"
            )
            if not original_billingo_id:
                frappe.throw(
                    f"Cannot create a Billingo credit note for {doc.name}: the original "
                    f"Sales Invoice {doc.return_against or '(missing)'} has no Billingo document ID."
                )

            response = requests.post(
                f"{BILLINGO_BASE_URL}/documents/{original_billingo_id}/create-modification-document",
                json=_build_modification_payload(doc),
                headers=_get_headers(),
                timeout=20,
            )
            response.raise_for_status()
            result = response.json()

            doc.db_set("custom_billingo_document_id", result.get("id"))
            doc.db_set("custom_billingo_invoice_number", result.get("invoice_number"))
            doc.db_set("custom_billingo_sync_status", "Synced")
            doc.db_set("custom_billingo_error", "")
            return

        billingo_id = doc.get("custom_billingo_document_id")
        partner_id = _get_or_create_partner(doc.customer)
        payload = _build_billingo_payload(doc, partner_id, "invoice")

        if billingo_id:
            # PUT /documents/{id} converts the draft into an invoice -
            # same Billingo id, now finalized and numbered.
            response = requests.put(
                f"{BILLINGO_BASE_URL}/documents/{billingo_id}",
                json=payload,
                headers=_get_headers(),
                timeout=20,
            )
        else:
            # No draft was pushed (for example, the invoice was imported),
            # so create a real invoice directly.
            existing_document = _get_document_by_vendor_id(doc.name)
            if existing_document:
                _mark_document_synced(doc, existing_document)
                return
            response = requests.post(
                f"{BILLINGO_BASE_URL}/documents",
                json=payload,
                headers=_get_headers(),
                timeout=20,
            )
        response.raise_for_status()
        result = response.json()

        _mark_document_synced(doc, result)

    except requests.exceptions.HTTPError as e:
        error_detail = _get_error_text(e.response) if e.response is not None else str(e)
        _raise_finalization_error(doc, error_detail)

    except Exception as e:
        _raise_finalization_error(doc, f"{e}\n\n{frappe.get_traceback()}")


@frappe.whitelist()
def retry_billingo_sync(sales_invoice: str) -> dict:
    """Manually retry Billingo finalization for a submitted failed invoice."""
    doc = frappe.get_doc("Sales Invoice", sales_invoice)
    doc.check_permission("write")

    if doc.docstatus != 1:
        frappe.throw("Only submitted Sales Invoices can be retried.")
    if doc.get("custom_billingo_sync_status") != "Failed":
        frappe.throw("Billingo retry is only available when the sync status is Failed.")

    finalize_billingo_invoice(doc)
    return {
        "document_id": doc.get("custom_billingo_document_id"),
        "invoice_number": doc.get("custom_billingo_invoice_number"),
    }
