"""
Billingo <-> ERPNext integration.

Flow on Sales Invoice submit:
1. Ensure the ERPNext Customer has a matching Billingo Partner
   (create one if we haven't synced it before; store the Billingo
   partner id on the Customer so we don't create duplicates).
2. Create a Billingo Document (invoice) referencing that partner,
   built from the Sales Invoice's items.
3. Store the returned Billingo invoice id/number back on the
   Sales Invoice.

Confirmed against Billingo API v3 sandbox on 2026-08-30:
- POST /v3/partners  -> 201, returns partner object with "id"
- POST /v3/documents -> 201, returns document object with "id" and
  "invoice_number". payment_method must be "wire_transfer" (not
  "bank_transfer"); vat must be a string like "27%"; block_id can be
  0 / omitted and Billingo will use the account's default block.
"""

import frappe
import requests

BILLINGO_BASE_URL = "https://api.billingo.hu/v3"


def _get_headers():
    api_key = frappe.conf.get("billingo_api_key")
    if not api_key:
        frappe.throw("billingo_api_key is not set in site_config.json")
    return {
        "X-API-KEY": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _get_or_create_partner(customer_name):
    """
    Look up (or create) the Billingo partner id for an ERPNext Customer.
    The id is cached on the Customer via a custom field so repeat
    invoices for the same customer don't create duplicate partners.
    """
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

    # cache it on the customer so we don't recreate it next time
    frappe.db.set_value("Customer", customer_name, "custom_billingo_partner_id", partner_id)
    frappe.db.commit()

    return partner_id


def _get_customer_address(customer_name):
    """Best-effort pull of the customer's primary address."""
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


def _map_items(sales_invoice):
    """
    Map Sales Invoice line items to Billingo's inline-item format
    (no pre-existing Billingo product required).
    """
    items = []
    for row in sales_invoice.items:
        items.append({
            "name": row.item_name or row.item_code,
            "unit_price": row.rate,
            "unit_price_type": "net",
            "quantity": row.qty,
            "unit": row.uom or "pcs",
            "vat": _map_vat_rate(row),
            "comment": row.description or "",
        })
    return items


def _map_vat_rate(row):
    """
    Very simple VAT mapping placeholder: read the item's tax rate
    off the Sales Invoice if you're using item-level taxes, or
    hardcode/derive from your tax template. Adjust to your setup.
    """
    # TODO: replace with real logic pulling from your Sales Taxes and Charges table
    return "27%"


def create_billingo_invoice(doc, method):
    """doc_events hook: Sales Invoice on_submit"""
    try:
        partner_id = _get_or_create_partner(doc.customer)

        payload = {
            "partner_id": partner_id,
            "block_id": 0,
            "type": "invoice",
            "fulfillment_date": str(doc.posting_date),
            "due_date": str(doc.due_date or doc.posting_date),
            "payment_method": "wire_transfer",
            "language": "en",
            "currency": doc.currency or "EUR",
            "conversion_rate": doc.conversion_rate or 1,
            "electronic": False,
            "paid": False,
            "items": _map_items(doc),
            "comment": f"ERPNext Sales Invoice {doc.name}",
        }

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
        doc.db_set("custom_billingo_sync_status", "Synced")

    except requests.exceptions.HTTPError as e:
        error_detail = e.response.text if e.response is not None else str(e)
        doc.db_set("custom_billingo_sync_status", "Failed")
        doc.db_set("custom_billingo_error", error_detail[:500])
        frappe.log_error(error_detail, "Billingo Sync Error")

    except Exception as e:
        doc.db_set("custom_billingo_sync_status", "Failed")
        doc.db_set("custom_billingo_error", str(e)[:500])
        frappe.log_error(frappe.get_traceback(), "Billingo Sync Error")
