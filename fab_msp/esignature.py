"""Electronic signature of the Trison parte, through OpenAPI's eSignature service.

The technician closes the intervention and sends the parte for signature. The app
renders the same PDF the form prints, opens one SES request carrying both signers
(the technician and the shop manager) and the two signature boxes of the paper
form, and hands back a link each of them signs with an OTP. When both have
signed, the signed parte and its audit trail come back onto the task.

The request/response mapping follows the published OAS3 spec,
https://console.openapi.com/oas/en/esignature.openapi.json
(POST /EU-SES, GET /signatures/{id}/{actionType}). The account, the endpoint and
the OAuth token are not ours: they belong to an OpenAPI Connection in fab_openapi,
which already mints and caches the token for every OpenAPI service.
"""

from __future__ import annotations

import base64
import hmac
import io
import json
from typing import Any, Mapping

import frappe
import requests
from fab_openapi.clients.base import OpenAPIClient, extract_api_data, extract_error_message
from frappe import _
from frappe.utils import (
    add_to_date,
    cint,
    escape_html,
    formatdate,
    get_datetime,
    get_url,
    get_url_to_form,
    now_datetime,
)

SETTINGS = "MSP Signature Settings"
CONNECTION_DOCTYPE = "OpenAPI Connection"
SERVICE_TYPE = "eSignature"

DEFAULT_SIGNATURE_TYPE = "EU-SES"
SIGNATURES_PATH = "/signatures"

TECHNICIAN, MANAGER = "technician", "manager"


# --- where the two signatures land on the parte -----------------------------
#
# A signature box is `page` + `x` from the left + `y` from the top. The spec
# gives no unit, but it reports page geometry as 595x842 (A4 in PDF points) and
# its own example is x=331 y=45, so the boxes below are in points, and y is read
# as the top of the stamp. The stamp itself is the documented 164x45 default.
#
# The parte prints the two boxes side by side under the FIRMA INSTALADOR /
# FIRMA ENCARGADO labels (see print_formats/trison_parte_de_ticket.html), so the
# boxes are found by that label: a longer material or travel table pushes the
# whole block down the page. The fallback numbers are the block's position on a
# one-page parte, measured on a Chrome-rendered PDF.
SIGNATURE_ANCHOR = "FIRMA INSTALADOR"
FALLBACK_ANCHOR_PAGE = 1
FALLBACK_ANCHOR_BASELINE_PT = 688.5
A4_WIDTH_PT = 595.9
PAGE_MARGIN_PT = 22.7  # the 8mm print margin of the parte
STAMP_WIDTH_PT = 164
STAMP_HEIGHT_PT = 45
ANCHOR_TO_BOX_TOP_PT = 4.5  # label baseline down to the top of the signing row
SIGN_ROW_HEIGHT_PT = 51  # the 18mm of .tp-sign, in points


def signature_boxes(page: int, baseline: float, page_width: float) -> list[dict]:
    """The two signature boxes of one parte page, technician first.

    The signing row runs the full width between the print margins and is split in
    two equal cells; the stamp is centred in its own cell.
    """
    column_width = (page_width - 2 * PAGE_MARGIN_PT) / 2
    top = baseline + ANCHOR_TO_BOX_TOP_PT + (SIGN_ROW_HEIGHT_PT - STAMP_HEIGHT_PT) / 2
    return [
        {
            "page": page,
            "x": str(round(PAGE_MARGIN_PT + column * column_width + (column_width - STAMP_WIDTH_PT) / 2)),
            "y": str(round(top)),
        }
        for column in (0, 1)
    ]


def signature_positions(pdf_bytes: bytes) -> list[dict]:
    anchor = _anchor_position(pdf_bytes, SIGNATURE_ANCHOR)
    if not anchor:
        return signature_boxes(FALLBACK_ANCHOR_PAGE, FALLBACK_ANCHOR_BASELINE_PT, A4_WIDTH_PT)
    return signature_boxes(*anchor)


def _anchor_position(pdf_bytes: bytes, needle: str):
    """Page, baseline from the top and width of the page carrying `needle`.

    None when the text cannot be found, so the caller falls back to the measured
    constants rather than refusing to send the parte.
    """
    # the renderer may kern the label into one TJ array, and pypdf then hands the
    # visitor the words with the spaces dropped, so both sides lose their spaces
    wanted = "".join(needle.split())
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(pdf_bytes))
        for index, page in enumerate(reader.pages, start=1):
            height = float(page.mediabox.height)
            found = []

            def visitor(text, cm, tm, font_dict, font_size, found=found, height=height):
                if wanted in "".join(text.split()):
                    found.append(height - (cm[3] * tm[5] + cm[5]))

            page.extract_text(visitor_text=visitor)
            if found:
                return index, found[0], float(page.mediabox.width)
    except Exception:
        frappe.log_error(title="MSP signature: could not read the parte back")
        return None
    # the boxes still get placed, on the measured constants, but a parte whose
    # layout moved would sign in the wrong spot: say so rather than sign blind
    frappe.log_error(
        message=f"{needle} not found in the rendered parte; the signature boxes fall back to the "
        f"measured position (page {FALLBACK_ANCHOR_PAGE}, baseline {FALLBACK_ANCHOR_BASELINE_PT}).",
        title="MSP signature: signature boxes not located",
    )
    return None


# --- signers ----------------------------------------------------------------

# the OTP message rides an SMS, so it stays under the documented 160 characters
OTP_MESSAGE = "{sender}: parte {parte}. OTP {OTP}"
AUTHENTICATION = {"SMS": "sms", "Email": "email"}


def split_name(full_name: str) -> tuple[str, str]:
    parts = (full_name or "").split()
    if len(parts) < 2:
        frappe.throw(
            _("{0} is not a full name: the signature needs a first name and a surname.").format(
                full_name or _("(empty)")
            )
        )
    return parts[0], " ".join(parts[1:])


def build_signers(technician: dict, manager: dict, positions: list[dict], settings) -> list[dict]:
    """The two signers of a parte in the shape POST /EU-SES takes.

    Order is technician then manager, matching the two boxes of the paper form
    left to right, and the order the signing links are stored back in.
    """
    channel = (settings.get("otp_channel") or "SMS").strip()
    method = AUTHENTICATION.get(channel, "sms")
    # sms sends the OTP to the mobile, email to the address: no contact, no signer
    required = "mobile" if method == "sms" else "email"
    language = (settings.get("signer_language") or "es").strip()
    sender = (settings.get("sender_name") or "").strip() or "Fabricators"
    parte = technician.get("parte") or ""

    signers = []
    for who, position in zip((technician, manager), positions):
        name, surname = split_name(who.get("full_name"))
        if not who.get(required):
            frappe.throw(
                _("{0} has no {1} to send the OTP to.").format(who.get("label") or name, required)
            )
        signer = {
            "name": name,
            "surname": surname,
            "authentication": [method],
            "signatures": [position],
            "language": language,
            "message": OTP_MESSAGE.format(sender=sender, parte=parte, OTP="{OTP}"),
        }
        for field in ("email", "mobile"):
            if who.get(field):
                signer[field] = who[field]
        signers.append(signer)
    return signers


def technician_contact(doc) -> dict:
    """Who signs the FIRMA INSTALADOR box: the operator the parte already names."""
    from fab_msp.field_service import operator_name

    contact = {"label": _("The technician"), "full_name": operator_name(doc), "parte": doc.get("fab_fs_parte")}
    if doc.get("fab_fs_technician_kind") == "External" and doc.get("fab_fs_supplier"):
        contact.update(_supplier_contact(doc.fab_fs_supplier))
    elif doc.get("fab_fs_employee"):
        employee = frappe.db.get_value(
            "Employee",
            doc.fab_fs_employee,
            ["employee_name", "cell_number", "personal_email", "prefered_email"],
            as_dict=True,
        )
        contact.update(
            {
                "full_name": employee.employee_name or contact["full_name"],
                "mobile": employee.cell_number,
                "email": employee.prefered_email or employee.personal_email,
            }
        )
    if doc.get("fab_fs_technician_user") and not (contact.get("email") and contact.get("mobile")):
        user = frappe.db.get_value(
            "User", doc.fab_fs_technician_user, ["email", "mobile_no", "full_name"], as_dict=True
        )
        for field, value in (("email", user.email), ("mobile", user.mobile_no), ("full_name", user.full_name)):
            contact[field] = contact.get(field) or value
    return contact


def _supplier_contact(supplier: str) -> dict:
    name = frappe.db.get_value(
        "Dynamic Link",
        {"link_doctype": "Supplier", "link_name": supplier, "parenttype": "Contact"},
        "parent",
    )
    if not name:
        return {}
    contact = frappe.db.get_value(
        "Contact", name, ["first_name", "last_name", "email_id", "mobile_no"], as_dict=True
    )
    full_name = " ".join(p for p in (contact.first_name, contact.last_name) if p)
    return {
        "full_name": full_name or None,
        "email": contact.email_id,
        "mobile": contact.mobile_no,
    }


def manager_contact(doc) -> dict:
    """Who signs the FIRMA ENCARGADO box: the shop manager the technician typed in."""
    return {
        "label": _("The shop manager"),
        "full_name": doc.get("fab_fs_manager_name"),
        "email": doc.get("fab_fs_manager_email"),
        "mobile": doc.get("fab_fs_manager_phone"),
        "parte": doc.get("fab_fs_parte"),
    }


# --- state ------------------------------------------------------------------

# The provider's own states are WAIT_VALIDATION, WAIT_SIGNERS, DONE and ERROR
# (GET /signatures/{id}/detail; the spec calls the middle one WAIT_SIGN, the
# sandbox answers WAIT_SIGNERS, so nothing here matches on it by name). The detail
# payload also carries the `signers` array with a per-signer state, which is what
# tells a half-signed parte from a fresh one.
SIGNED_SIGNER_STATES = {"DONE", "SIGNED", "COMPLETED"}
DECLINED_MARKERS = ("REFUS", "DECLIN", "REJECT")
PENDING_STATES = ("Sent", "Partially signed")


def signer_states(payload: Mapping[str, Any]) -> list[str]:
    return [str(s.get("state") or "").upper() for s in (payload.get("signers") or [])]


def map_state(payload: Mapping[str, Any], days_validity: int = 0, now=None) -> str:
    """The task's signature status for a provider payload.

    Declined and Expired are ours: the provider documents neither, so a refusal is
    read off whatever state word it uses, and a request left unsigned past the
    configured validity is called expired here rather than waited on for ever.
    """
    state = str(payload.get("state") or "").upper()
    states = signer_states(payload)
    if state == "DONE":
        return "Signed"
    if state == "ERROR":
        return "Error"
    if any(marker in word for word in [state, *states] for marker in DECLINED_MARKERS):
        return "Declined"
    if days_validity and _older_than(payload.get("createdAt"), days_validity, now):
        return "Expired"
    if any(word in SIGNED_SIGNER_STATES for word in states):
        return "Partially signed"
    return "Sent"


def _older_than(created_at, days: int, now=None) -> bool:
    created = _as_datetime(created_at)
    if not created:
        return False
    return created < add_to_date(now or now_datetime(), days=-cint(days))


def _as_datetime(value):
    try:
        return get_datetime(str(value).split("+")[0].strip()) if value else None
    except (ValueError, TypeError):
        return None


def signing_links(payload: Mapping[str, Any]) -> dict:
    """The link each signer opens, by role. Signers come back in the order sent."""
    urls = [s.get("url") for s in (payload.get("signers") or [])]
    return {role: url for role, url in zip((TECHNICIAN, MANAGER), urls) if url}


# --- client -----------------------------------------------------------------


class ESignatureClient(OpenAPIClient):
    """Every HTTP call of the module.

    The connection, its endpoint and its OAuth token come from fab_openapi, which
    holds the OpenAPI account; what belongs here is the signature endpoint, the
    scopes it needs and the three calls the parte makes.
    """

    service_type = SERVICE_TYPE

    def __init__(self, connection=None, signature_type: str | None = None):
        super().__init__(connection if connection is not None else get_connection())
        self.signature_type = (signature_type or DEFAULT_SIGNATURE_TYPE).strip()

    def signature_path(self) -> str:
        """The endpoint is the signature level itself, POST /EU-SES."""
        return f"/{self.signature_type}"

    def token_scope_requests(self) -> tuple[tuple[str, str], ...]:
        return (("POST", self.signature_path()), ("GET", SIGNATURES_PATH))

    def _call(self, method: str, path: str, accept="application/json", **kwargs):
        url = self.build_url(path)
        for attempt in range(2):
            headers = {
                "Accept": accept,
                "Authorization": self.get_authorization_header(
                    (path,), method=method, force_refresh=attempt > 0
                ),
            }
            try:
                response = requests.request(
                    method, url, headers=headers, timeout=self.get_timeout_seconds(), **kwargs
                )
            except requests.RequestException as exc:
                frappe.throw(_("The eSignature request to {0} failed: {1}").format(url, exc))
            if self._should_retry_auth(response.status_code, attempt):
                self.invalidate_access_token()
                continue
            break
        if response.status_code >= 400:
            frappe.throw(
                _("The eSignature request to {0} failed with status {1}: {2}").format(
                    url, response.status_code, extract_error_message(_json_or_none(response), response.text)
                )
            )
        return response

    def create_ses_request(self, pdf_bytes: bytes, signers: list[dict], callback_url=None, callback_data=None) -> dict:
        """POST /EU-SES: the document, its signers and where each signature goes.

        The parte travels as base64 rather than a url: it is a private attachment
        of the task, so there is nothing the provider could fetch.
        """
        payload = {
            "signers": signers,
            "inputDocuments": [
                {"sourceType": "base64", "payload": base64.b64encode(pdf_bytes).decode("ascii")}
            ],
            "options": {"signatureMode": ["typed", "drawn"], "signerMustRead": True},
        }
        if callback_url:
            payload["callback"] = {
                "method": "JSON",
                "url": callback_url,
                "retry": 3,
                "custom": callback_data or {},
            }
        return _json(self._call("POST", self.signature_path(), json=payload))

    def get_status(self, request_id: str) -> dict:
        return _json(self._call("GET", f"{SIGNATURES_PATH}/{request_id}/detail"))

    def download(self, request_id: str, action_type: str) -> bytes:
        accept = "application/pdf" if action_type == "audit" else "application/octet-stream"
        return self._call("GET", f"{SIGNATURES_PATH}/{request_id}/{action_type}", accept=accept).content


def _json(response) -> dict:
    """The signature object itself: every answer arrives inside the OpenAPI
    envelope, and the documented schemas describe what the envelope carries."""
    try:
        payload = extract_api_data(response.json())
    except ValueError:
        frappe.throw(
            _("The eSignature service answered status {0} with something that is not JSON.").format(
                response.status_code
            )
        )
    if not isinstance(payload, dict):
        frappe.throw(_("The eSignature service answered {0} with no signature object.").format(response.status_code))
    return payload


def _json_or_none(response):
    try:
        return response.json()
    except ValueError:
        return None


# --- settings ---------------------------------------------------------------


def get_settings():
    return frappe.get_cached_doc(SETTINGS)


def get_connection(settings=None):
    """The OpenAPI Connection the signature runs on. The account and the token are
    fab_openapi's business, so the settings only point at the row."""
    settings = settings if settings is not None else get_settings()
    connection = (settings.get("connection") or "").strip()
    if not connection:
        frappe.throw(
            _("Set the OpenAPI connection on {0}: it carries the account and the endpoint.").format(
                _(SETTINGS)
            )
        )
    return frappe.get_cached_doc(CONNECTION_DOCTYPE, connection)


def get_secret(settings, fieldname) -> str | None:
    """The decrypted value of a Password field, also for the dicts the tests
    build in place of the doc."""
    if isinstance(settings, Mapping):
        value = settings.get(fieldname)
    else:
        get_password = getattr(settings, "get_password", None)
        value = get_password(fieldname, raise_exception=False) if callable(get_password) else None
    return str(value).strip() if value else None


def is_enabled() -> bool:
    return bool(frappe.db.exists("DocType", SETTINGS)) and bool(
        frappe.db.get_single_value(SETTINGS, "enabled")
    )


def get_client(settings=None):
    settings = settings if settings is not None else get_settings()
    return ESignatureClient(
        get_connection(settings), signature_type=settings.get("signature_type")
    )


# --- the task's signature ---------------------------------------------------

SENDABLE_STATES = ("", None, "Not sent", "Error", "Expired", "Declined")


@frappe.whitelist(methods=["POST"])
def send_for_signature(task: str) -> dict:
    """Render the parte and open one signature request for both signers.

    The technician runs this from their own task, so the check is the plain write
    permission on the task: the technician query conditions already keep them to
    their own jobs.
    """
    doc = frappe.get_doc("Task", task)
    doc.check_permission("write")
    # two taps must not open two requests, each with its own OTP to the manager
    frappe.db.get_value("Task", task, "name", for_update=True)
    doc.reload()
    if not doc.get("fab_fs_parte"):
        frappe.throw(_("This task is not an intervention."))
    if doc.get("status") != "Completed":
        frappe.throw(_("Complete the intervention before sending the parte for signature."))
    if doc.get("fab_fs_signature_status") not in SENDABLE_STATES:
        frappe.throw(
            _("The parte is already {0}.").format(_(doc.get("fab_fs_signature_status")))
        )
    if not is_enabled():
        frappe.throw(_("Electronic signature is off: enable it on {0}.").format(_(SETTINGS)))

    settings = get_settings()
    pdf = render_parte(doc)
    signers = build_signers(
        technician_contact(doc), manager_contact(doc), signature_positions(pdf), settings
    )
    payload = get_client(settings).create_ses_request(
        pdf,
        signers,
        callback_url=callback_url(),
        callback_data={"task": doc.name, "secret": get_secret(settings, "callback_secret")},
    )
    request_id = payload.get("id")
    if not request_id:
        frappe.throw(_("The eSignature service returned no request id."))

    links = signing_links(payload)
    doc.db_set(
        {
            "fab_fs_signature_id": request_id,
            "fab_fs_signature_status": map_state(payload),
            "fab_fs_signature_links": json.dumps(links, indent=1),
        }
    )
    doc.add_comment("Comment", _("Parte sent for signature ({0}).").format(request_id))
    return {"id": request_id, "links": links, "qr": {role: qr_data_uri(url) for role, url in links.items()}}


def render_parte(doc) -> bytes:
    from fab_msp.field_service import PDF_GENERATOR, PRINT_FORMAT
    from frappe.utils.print_utils import get_print

    return get_print(
        "Task",
        doc.name,
        print_format=PRINT_FORMAT,
        as_pdf=True,
        no_letterhead=1,
        pdf_generator=PDF_GENERATOR,
    )


def callback_url() -> str:
    return f"{get_url()}/api/method/fab_msp.esignature.receive_signature_callback"


def qr_data_uri(url: str) -> str | None:
    """The manager signs on their own phone, so the link is also shown as a QR
    code the technician holds out to them."""
    try:
        import pyqrcode

        stream = io.BytesIO()
        pyqrcode.create(url).png(stream, scale=5, quiet_zone=2)
        return "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode("ascii")
    except Exception:
        return None


@frappe.whitelist()
def get_signing_links(task: str) -> dict:
    """The stored links and their QR codes, so the manager can still be handed the
    code when the technician comes back to the task later."""
    doc = frappe.get_doc("Task", task)
    doc.check_permission("read")
    links = _as_dict(doc.get("fab_fs_signature_links")) or {}
    return {"links": links, "qr": {role: qr_data_uri(url) for role, url in links.items()}}


@frappe.whitelist(methods=["POST"])
def refresh_signature(task: str) -> str:
    doc = frappe.get_doc("Task", task)
    doc.check_permission("write")
    if not doc.get("fab_fs_signature_id"):
        frappe.throw(_("This parte was never sent for signature."))
    payload = get_client().get_status(doc.fab_fs_signature_id)
    return apply_signature_state(doc.name, payload)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def receive_signature_callback(**kwargs) -> str:
    """The provider's callback: same work as a refresh, without the poll.

    Guest endpoint, so the shared secret we put in the request's `custom` data is
    what says the call is ours.
    """
    data = _as_dict(frappe.form_dict.get("data")) or {}
    custom = _as_dict(frappe.form_dict.get("custom")) or {}
    secret = get_secret(get_settings(), "callback_secret")
    if not secret or not hmac.compare_digest(str(custom.get("secret") or ""), secret):
        raise frappe.PermissionError

    task = custom.get("task")
    if not task or not frappe.db.exists("Task", task):
        task = frappe.db.get_value("Task", {"fab_fs_signature_id": data.get("id")}, "name")
    if not task:
        return "unknown"
    return apply_signature_state(task, data)


def _as_dict(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def apply_signature_state(task: str, payload: Mapping[str, Any]) -> str:
    """Move the task onto the state the provider reports. Idempotent: the signed
    parte, the audit and the notification happen once, whatever the callback does."""
    settings = get_settings()
    state = map_state(payload, cint(settings.get("days_validity")))
    # serialise two callbacks landing together on the same task
    frappe.db.get_value("Task", task, "name", for_update=True)
    doc = frappe.get_doc("Task", task)
    request_id = payload.get("id")
    if request_id and doc.get("fab_fs_signature_id") not in (None, "", str(request_id)):
        # a late callback of a request that was already replaced by a new send:
        # applying it would overwrite the live one and attach the wrong parte
        return "stale"
    values = {"fab_fs_signature_status": state}
    links = signing_links(payload)
    if links:
        values["fab_fs_signature_links"] = json.dumps(links, indent=1)

    completing = state == "Signed" and not doc.get("fab_fs_signed_parte")
    if completing:
        values.update(store_signed_documents(doc, payload))
        values["fab_fs_signed_on"] = now_datetime()
    doc.db_set(values)
    if completing:
        doc.add_comment("Comment", _("Parte signed by both signers."))
        notify_signed(doc, settings)
    return state


def store_signed_documents(doc, payload: Mapping[str, Any]) -> dict:
    """The signed parte and its audit trail, attached privately to the task."""
    client = get_client()
    request_id = payload.get("id") or doc.get("fab_fs_signature_id")
    parte = (doc.get("fab_fs_parte") or doc.name).replace("/", "-")
    files = {
        "fab_fs_signed_parte": (f"{parte} signed.pdf", client.download(request_id, "signedDocument")),
        "fab_fs_signature_audit": (f"{parte} audit.pdf", client.download(request_id, "audit")),
    }
    return {field: attach_private(doc, field, *value) for field, value in files.items()}


def attach_private(doc, fieldname: str, file_name: str, content: bytes) -> str:
    file = frappe.get_doc(
        {
            "doctype": "File",
            "file_name": file_name,
            "attached_to_doctype": doc.doctype,
            "attached_to_name": doc.name,
            "attached_to_field": fieldname,
            "is_private": 1,
            "content": content,
        }
    )
    file.insert(ignore_permissions=True)
    return file.file_url


# the signed parte goes to our own inbox, not to the customer: Trison gets the
# parte through their own channel
NOTIFY_SUBJECT = "Signed parte {parte} - {end_customer}"
NOTIFY_BODY = (
    "<p>The parte {parte} of {date} at {end_customer} has been signed by both signers.</p>"
    "<p>Technician: {technician}</p>"
    '<p><a href="{link}">{task}</a></p>'
)


def notify_signed(doc, settings=None) -> None:
    settings = settings or get_settings()
    recipient = (settings.get("notify_email") or "").strip()
    if not recipient:
        return
    from fab_msp.field_service import operator_name

    date = doc.get("fab_fs_intervention_date")
    context = {
        "parte": doc.get("fab_fs_parte") or doc.name,
        "end_customer": doc.get("fab_fs_end_customer") or "",
        "date": formatdate(date) if date else "",
        "technician": operator_name(doc),
        "task": doc.name,
        "link": get_url_to_form("Task", doc.name),
    }
    # the parte and the end customer are typed by hand, so they never reach the
    # html of the mail unescaped
    body = {key: escape_html(str(value)) for key, value in context.items()}
    frappe.sendmail(
        recipients=[recipient],
        subject=NOTIFY_SUBJECT.format(**context),
        message=NOTIFY_BODY.format(**body),
        attachments=[
            {"file_url": url}
            for url in (doc.get("fab_fs_signed_parte"), doc.get("fab_fs_signature_audit"))
            if url
        ],
        reference_doctype="Task",
        reference_name=doc.name,
    )


def poll_pending_signatures(minutes: int = 15) -> list[str]:
    """Catch the requests whose callback never arrived, and expire the stale ones."""
    if not is_enabled():
        return []
    cutoff = add_to_date(now_datetime(), minutes=-cint(minutes))
    tasks = frappe.get_all(
        "Task",
        filters={
            "fab_fs_signature_status": ["in", PENDING_STATES],
            "fab_fs_signature_id": ["is", "set"],
            "modified": ["<", cutoff],
        },
        fields=["name", "fab_fs_signature_id"],
    )
    client = get_client()
    moved = []
    for task in tasks:
        try:
            payload = client.get_status(task.fab_fs_signature_id)
            if apply_signature_state(task.name, payload) not in PENDING_STATES:
                moved.append(task.name)
            frappe.db.commit()  # one bad request must not undo the ones before it
        except Exception:
            frappe.db.rollback()
            frappe.log_error(title=f"MSP signature: could not refresh {task.name}")
    return moved


# --- setup ------------------------------------------------------------------


def task_custom_fields() -> list[dict]:
    """The signature block of the parte, merged into the field service fields."""
    return [
        {
            "fieldname": "fab_fs_signature_section",
            "label": "Signature",
            "fieldtype": "Section Break",
            "insert_after": "fab_fs_travel",
            "depends_on": "fab_fs_parte",
        },
        {
            "fieldname": "fab_fs_manager_name",
            "label": "Shop Manager",
            "fieldtype": "Data",
            "insert_after": "fab_fs_signature_section",
            "description": "Who signs the FIRMA ENCARGADO box. First name and surname.",
        },
        {
            "fieldname": "fab_fs_manager_phone",
            "label": "Shop Manager Phone",
            "fieldtype": "Data",
            "insert_after": "fab_fs_manager_name",
            "description": "International format, e.g. +34600111222: the OTP is sent here.",
        },
        {
            "fieldname": "fab_fs_manager_email",
            "label": "Shop Manager Email",
            "fieldtype": "Data",
            "options": "Email",
            "insert_after": "fab_fs_manager_phone",
        },
        {
            "fieldname": "fab_fs_col_signature",
            "fieldtype": "Column Break",
            "insert_after": "fab_fs_manager_email",
        },
        {
            "fieldname": "fab_fs_signature_status",
            "label": "Signature Status",
            "fieldtype": "Select",
            "options": "Not sent\nSent\nPartially signed\nSigned\nDeclined\nExpired\nError",
            "default": "Not sent",
            "read_only": 1,
            "insert_after": "fab_fs_col_signature",
            "in_standard_filter": 1,
        },
        {
            "fieldname": "fab_fs_signature_id",
            "label": "Signature Request",
            "fieldtype": "Data",
            "read_only": 1,
            "insert_after": "fab_fs_signature_status",
        },
        {
            "fieldname": "fab_fs_signed_on",
            "label": "Signed On",
            "fieldtype": "Datetime",
            "read_only": 1,
            "insert_after": "fab_fs_signature_id",
        },
        {
            "fieldname": "fab_fs_signature_links",
            "label": "Signing Links",
            "fieldtype": "Small Text",
            "read_only": 1,
            "insert_after": "fab_fs_signed_on",
        },
        {
            "fieldname": "fab_fs_signed_parte",
            "label": "Signed Parte",
            "fieldtype": "Attach",
            "read_only": 1,
            "insert_after": "fab_fs_signature_links",
        },
        {
            "fieldname": "fab_fs_signature_audit",
            "label": "Signature Audit",
            "fieldtype": "Attach",
            "read_only": 1,
            "insert_after": "fab_fs_signed_parte",
        },
    ]
