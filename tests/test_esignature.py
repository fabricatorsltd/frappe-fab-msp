import json
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from frappe.utils import get_datetime

from fab_msp import esignature

# the tests run without a site, and now_datetime() would go looking for the
# system timezone, so the clock is fixed wherever the module reads it
NOW = get_datetime("2026-09-04 12:00:00")

# the fallback test hands pypdf something that is not a pdf on purpose
logging.getLogger("pypdf").setLevel(logging.CRITICAL)


class Thrown(Exception):
    """What the patched frappe.throw raises, so a refusal is assertable."""


def frappe_stub(**overrides):
    stub = SimpleNamespace(
        throw=_throw,
        db=SimpleNamespace(exists=lambda *a, **k: True),
        log_error=lambda *a, **k: None,
    )
    for key, value in overrides.items():
        setattr(stub, key, value)
    return stub


def _throw(message, *args, **kwargs):
    raise Thrown(message)


def settings(**overrides):
    values = {
        "connection": "eSignature Sandbox",
        "signature_type": "EU-SES",
        "otp_channel": "SMS",
        "signer_language": "es",
        "sender_name": "Fabricators",
        "days_validity": 30,
    }
    values.update(overrides)
    return values


def connection(**overrides):
    """The OpenAPI Connection the client is built from. Bearer Token mode, so the
    transport tests never reach the token endpoint: minting is fab_openapi's own."""
    values = {
        "connection_name": "eSignature Sandbox",
        "environment": "Sandbox",
        "endpoint_url": "",
        "status_url": "",
        "oauth_token_url": "",
        "auth_mode": "Bearer Token",
        "timeout_seconds": 30,
        "get_password": lambda fieldname, raise_exception=True: "tok-1",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


TECHNICIAN = {
    "label": "The technician",
    "full_name": "Marco Rossi",
    "mobile": "+393334455666",
    "email": "marco@example.com",
    "parte": "A26/015047",
}
MANAGER = {
    "label": "The shop manager",
    "full_name": "Ana Garcia Lopez",
    "mobile": "+34600111222",
    "email": "ana@example.com",
    "parte": "A26/015047",
}


class TestSignatureBoxes(unittest.TestCase):
    def test_the_two_boxes_sit_side_by_side_on_the_signing_row(self):
        technician, manager = esignature.signature_boxes(1, 688.5, 595.9)
        self.assertEqual(technician, {"page": 1, "x": "78", "y": "696"})
        self.assertEqual(manager["y"], technician["y"])
        # the manager's box is a full column to the right of the technician's
        self.assertAlmostEqual(
            int(manager["x"]) - int(technician["x"]), (595.9 - 2 * esignature.PAGE_MARGIN_PT) / 2, delta=1
        )

    def test_a_block_pushed_down_the_page_moves_both_boxes_down(self):
        higher = esignature.signature_boxes(1, 688.5, 595.9)
        lower = esignature.signature_boxes(2, 738.5, 595.9)
        self.assertEqual(int(lower[0]["y"]) - int(higher[0]["y"]), 50)
        self.assertEqual(lower[0]["page"], 2)

    def test_without_the_anchor_the_measured_constants_are_used(self):
        with patch.object(esignature, "frappe", frappe_stub()):
            self.assertEqual(
                esignature.signature_positions(b"not a pdf"),
                esignature.signature_boxes(
                    esignature.FALLBACK_ANCHOR_PAGE,
                    esignature.FALLBACK_ANCHOR_BASELINE_PT,
                    esignature.A4_WIDTH_PT,
                ),
            )


class TestSplitName(unittest.TestCase):
    def test_first_name_and_the_rest(self):
        self.assertEqual(esignature.split_name("Ana Garcia Lopez"), ("Ana", "Garcia Lopez"))

    def test_a_single_word_is_not_a_name_to_sign_with(self):
        with patch.object(esignature, "frappe", frappe_stub()):
            with self.assertRaises(Thrown):
                esignature.split_name("Ana")


class TestBuildSigners(unittest.TestCase):
    def _build(self, **overrides):
        positions = esignature.signature_boxes(1, 688.5, 595.9)
        return esignature.build_signers(TECHNICIAN, MANAGER, positions, settings(**overrides))

    def test_technician_first_then_manager_each_on_their_own_box(self):
        signers = self._build()
        self.assertEqual([s["name"] for s in signers], ["Marco", "Ana"])
        self.assertEqual(signers[0]["surname"], "Rossi")
        self.assertEqual(signers[1]["surname"], "Garcia Lopez")
        self.assertEqual(signers[0]["signatures"][0]["x"], "78")
        self.assertEqual(signers[1]["signatures"][0]["x"], "354")

    def test_sms_is_the_default_channel_and_both_contacts_travel(self):
        signer = self._build()[1]
        self.assertEqual(signer["authentication"], ["sms"])
        self.assertEqual(signer["mobile"], "+34600111222")
        self.assertEqual(signer["email"], "ana@example.com")

    def test_the_email_channel_switches_the_authentication(self):
        self.assertEqual(self._build(otp_channel="Email")[0]["authentication"], ["email"])

    def test_the_otp_message_names_the_parte_and_fits_an_sms(self):
        message = self._build()[0]["message"]
        self.assertIn("{OTP}", message)
        self.assertIn("A26/015047", message)
        self.assertLess(len(message), 160)

    def test_the_signer_language_comes_from_the_settings(self):
        self.assertEqual(self._build(signer_language="en")[0]["language"], "en")

    def test_a_manager_without_a_phone_cannot_be_sent_an_sms_otp(self):
        positions = esignature.signature_boxes(1, 688.5, 595.9)
        with patch.object(esignature, "frappe", frappe_stub()):
            with self.assertRaises(Thrown):
                esignature.build_signers(
                    TECHNICIAN, {**MANAGER, "mobile": None}, positions, settings()
                )


REQUEST_ID = "693957bddcc141c34e03fcd9"


def payload(state="WAIT_SIGN", signer_states=("NEW", "NEW"), created_at="2026-09-02 10:00:00.000+00:00"):
    return {
        "id": REQUEST_ID,
        "state": state,
        "createdAt": created_at,
        "signers": [
            {"state": s, "url": f"https://esign.openapi.com/{i}"}
            for i, s in enumerate(signer_states)
        ],
    }


class TestMapState(unittest.TestCase):
    def test_a_fresh_request_is_sent(self):
        self.assertEqual(esignature.map_state(payload("WAIT_VALIDATION")), "Sent")

    def test_one_signer_through_is_partially_signed(self):
        self.assertEqual(esignature.map_state(payload(signer_states=("DONE", "NEW"))), "Partially signed")

    def test_both_through_is_signed(self):
        self.assertEqual(esignature.map_state(payload("DONE", ("DONE", "DONE"))), "Signed")

    def test_a_provider_error_is_an_error(self):
        self.assertEqual(esignature.map_state(payload("ERROR")), "Error")

    def test_a_refusal_by_a_signer_is_declined(self):
        self.assertEqual(esignature.map_state(payload(signer_states=("REFUSED", "NEW"))), "Declined")

    def test_unsigned_past_the_validity_is_expired(self):
        now = get_datetime("2026-10-15 10:00:00")
        self.assertEqual(esignature.map_state(payload(), days_validity=30, now=now), "Expired")

    def test_the_validity_never_expires_a_signed_request(self):
        now = get_datetime("2026-10-15 10:00:00")
        self.assertEqual(
            esignature.map_state(payload("DONE", ("DONE", "DONE")), days_validity=30, now=now), "Signed"
        )

    def test_no_validity_configured_never_expires(self):
        self.assertEqual(esignature.map_state(payload(), days_validity=0), "Sent")


class TestSigningLinks(unittest.TestCase):
    def test_links_come_back_in_the_order_the_signers_were_sent(self):
        self.assertEqual(
            esignature.signing_links(payload()),
            {"technician": "https://esign.openapi.com/0", "manager": "https://esign.openapi.com/1"},
        )

    def test_a_payload_without_signers_has_no_links(self):
        self.assertEqual(esignature.signing_links({}), {})


class Response:
    """What the provider really answers: the signature object inside the OpenAPI
    envelope, or an error carrying its message at the top level."""

    def __init__(self, status_code=200, payload=None, content=b"", text="", envelope=True):
        self.status_code = status_code
        self._payload = (
            {"data": payload, "success": True, "message": "", "error": None}
            if envelope and payload is not None
            else payload
        )
        self.content = content
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class TestClient(unittest.TestCase):
    def client(self, **overrides):
        return esignature.ESignatureClient(connection(**overrides))

    def test_the_endpoint_follows_the_connection_environment(self):
        self.assertEqual(
            self.client().build_url("/EU-SES"), "https://test.esignature.openapi.com/EU-SES"
        )
        self.assertEqual(
            self.client(environment="Production").build_url("/EU-SES"),
            "https://esignature.openapi.com/EU-SES",
        )

    def test_one_token_covers_the_two_calls_the_parte_makes(self):
        self.assertEqual(
            sorted(self.client().full_scope_value().split()),
            [
                "GET:test.esignature.openapi.com/signatures",
                "POST:test.esignature.openapi.com/EU-SES",
            ],
        )

    def test_the_signature_type_picks_the_endpoint_and_its_scope(self):
        client = esignature.ESignatureClient(connection(), signature_type="EU-AES")
        self.assertEqual(client.signature_path(), "/EU-AES")
        self.assertIn("POST:test.esignature.openapi.com/EU-AES", client.full_scope_value())

    def test_the_ses_request_carries_the_pdf_the_signers_and_the_callback(self):
        calls = []

        def request(method, url, **kwargs):
            calls.append((method, url, kwargs))
            return Response(payload={"id": "sig-1", "state": "WAIT_VALIDATION"})

        signers = [{"name": "Marco", "surname": "Rossi"}]
        with patch.object(esignature.requests, "request", request):
            answer = self.client().create_ses_request(
                b"%PDF-1.4", signers, callback_url="https://erp/callback", callback_data={"task": "T1"}
            )
        method, url, kwargs = calls[0]
        self.assertEqual((method, url), ("POST", "https://test.esignature.openapi.com/EU-SES"))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer tok-1")
        self.assertEqual(kwargs["timeout"], 30)
        body = kwargs["json"]
        self.assertEqual(body["signers"], signers)
        self.assertEqual(body["inputDocuments"][0]["sourceType"], "base64")
        self.assertEqual(body["inputDocuments"][0]["payload"], "JVBERi0xLjQ=")
        self.assertEqual(body["callback"]["url"], "https://erp/callback")
        self.assertEqual(body["callback"]["custom"], {"task": "T1"})
        self.assertEqual(answer["id"], "sig-1")

    def test_a_rejected_token_is_reminted_once(self):
        answers = [Response(401, payload={}, envelope=False), Response(payload={"id": "sig-1"})]
        refreshes = []

        client = esignature.ESignatureClient(connection(auth_mode="OAuth Client Credentials"))
        with patch.object(esignature.requests, "request", lambda *a, **k: answers.pop(0)), \
             patch.object(client, "invalidate_access_token", lambda: None), \
             patch.object(
                 client,
                 "get_client_credentials_token",
                 lambda force_refresh=False: refreshes.append(force_refresh) or "t",
             ):
            client.get_status("sig-1")
        self.assertEqual(refreshes, [False, True])

    def test_a_failing_call_says_what_the_provider_answered(self):
        with patch.object(esignature, "frappe", frappe_stub()), \
             patch.object(
                 esignature.requests,
                 "request",
                 lambda *a, **k: Response(
                     422, payload={"success": False, "message": "array 'signers' is empty"}, envelope=False
                 ),
             ):
            with self.assertRaises(Thrown) as caught:
                self.client().create_ses_request(b"%PDF", [])
        self.assertIn("array 'signers' is empty", str(caught.exception))

    def test_the_openapi_envelope_is_unwrapped(self):
        # the live api answers {"data": {...}, "success": true}: the signature
        # object is what the rest of the module works on
        detail = {"id": "sig-1", "state": "WAIT_SIGNERS", "signers": [{"state": "NEW"}]}
        with patch.object(esignature.requests, "request", lambda *a, **k: Response(payload=detail)):
            self.assertEqual(self.client().get_status("sig-1"), detail)

    def test_an_answer_with_no_signature_object_is_refused(self):
        with patch.object(esignature, "frappe", frappe_stub()), \
             patch.object(esignature.requests, "request", lambda *a, **k: Response(payload=[1, 2])):
            with self.assertRaises(Thrown):
                self.client().get_status("sig-1")

    def test_a_download_returns_the_bytes(self):
        with patch.object(esignature.requests, "request", lambda *a, **k: Response(content=b"pdf-bytes")):
            self.assertEqual(self.client().download("sig-1", "signedDocument"), b"pdf-bytes")


class FakeTask:
    doctype = "Task"

    def __init__(self, **values):
        self.name = "TASK-2026-00002"
        self.values = {"fab_fs_parte": "A26/015047", "fab_fs_signature_id": REQUEST_ID, **values}
        self.comments = []

    def get(self, field, default=None):
        return self.values.get(field, default)

    def db_set(self, field, value=None):
        self.values.update(field if isinstance(field, dict) else {field: value})

    def add_comment(self, kind, text):
        self.comments.append(text)

    def check_permission(self, ptype):
        return True

    def reload(self):
        return self


class TestSendForSignature(unittest.TestCase):
    def _send(self, doc, enabled=True, answer=None):
        sent = {}

        def create_ses_request(pdf, signers, callback_url=None, callback_data=None):
            sent.update(pdf=pdf, signers=signers, url=callback_url, data=callback_data)
            return answer if answer is not None else payload("WAIT_VALIDATION")

        stub = frappe_stub(
            get_doc=lambda dt, name: doc,
            db=SimpleNamespace(get_value=lambda *a, **k: doc.name, exists=lambda *a, **k: True),
        )
        with patch.object(esignature, "frappe", stub), \
             patch.object(esignature, "is_enabled", lambda: enabled), \
             patch.object(esignature, "get_settings", lambda: settings(callback_secret="shh")), \
             patch.object(esignature, "get_secret", lambda s, f: s.get(f)), \
             patch.object(esignature, "render_parte", lambda doc: b"%PDF-1.4"), \
             patch.object(esignature, "signature_positions", lambda pdf: esignature.signature_boxes(1, 688.5, 595.9)), \
             patch.object(esignature, "technician_contact", lambda doc: TECHNICIAN), \
             patch.object(esignature, "manager_contact", lambda doc: MANAGER), \
             patch.object(esignature, "callback_url", lambda: "https://erp/callback"), \
             patch.object(esignature, "qr_data_uri", lambda url: "data:image/png;base64,x"), \
             patch.object(esignature, "get_client", lambda settings=None: SimpleNamespace(create_ses_request=create_ses_request)):
            return esignature.send_for_signature(doc.name), sent

    def task(self, **overrides):
        values = {"status": "Completed", "fab_fs_signature_status": "Not sent", "fab_fs_signature_id": None}
        values.update(overrides)
        return FakeTask(**values)

    def test_a_completed_intervention_is_sent_and_the_links_come_back(self):
        doc = self.task()
        answer, sent = self._send(doc)
        self.assertEqual(answer["id"], REQUEST_ID)
        self.assertEqual(answer["links"]["manager"], "https://esign.openapi.com/1")
        self.assertTrue(answer["qr"]["manager"])
        self.assertEqual(sent["data"], {"task": doc.name, "secret": "shh"})
        self.assertEqual([s["name"] for s in sent["signers"]], ["Marco", "Ana"])
        self.assertEqual(doc.values["fab_fs_signature_id"], REQUEST_ID)
        self.assertEqual(doc.values["fab_fs_signature_status"], "Sent")
        self.assertEqual(len(doc.comments), 1)

    def test_an_open_intervention_is_not_sent(self):
        with self.assertRaises(Thrown):
            self._send(self.task(status="Open"))

    def test_a_task_without_a_parte_is_not_an_intervention(self):
        with self.assertRaises(Thrown):
            self._send(self.task(fab_fs_parte=None))

    def test_a_parte_already_out_for_signature_is_not_sent_again(self):
        with self.assertRaises(Thrown):
            self._send(self.task(fab_fs_signature_status="Sent"))

    def test_a_failed_request_can_be_sent_again(self):
        answer, _sent = self._send(self.task(fab_fs_signature_status="Error"))
        self.assertEqual(answer["id"], REQUEST_ID)

    def test_signature_switched_off_refuses_before_anything_is_rendered(self):
        with self.assertRaises(Thrown):
            self._send(self.task(), enabled=False)

    def test_an_answer_without_a_request_id_is_refused(self):
        with self.assertRaises(Thrown):
            self._send(self.task(), answer={"state": "WAIT_VALIDATION"})


class TestApplySignatureState(unittest.TestCase):
    def _run(self, doc, api_payload, notify_email="office@fabricators.dev"):
        mails = []
        downloads = []

        client = SimpleNamespace(
            download=lambda request_id, action: downloads.append(action) or b"bytes"
        )
        stub = frappe_stub(
            get_doc=lambda dt, name: doc,
            db=SimpleNamespace(get_value=lambda *a, **k: doc.name, exists=lambda *a, **k: True),
            sendmail=lambda **kwargs: mails.append(kwargs),
        )
        with patch.object(esignature, "frappe", stub), \
             patch.object(esignature, "now_datetime", lambda: NOW), \
             patch.object(esignature, "get_settings", lambda: settings(notify_email=notify_email)), \
             patch.object(esignature, "get_client", lambda settings=None: client), \
             patch.object(esignature, "attach_private", lambda doc, field, name, content: f"/private/{name}"), \
             patch("fab_msp.field_service.operator_name", lambda doc: "Marco Rossi"), \
             patch.object(esignature, "get_url_to_form", lambda dt, name: f"https://erp/app/task/{name}"), \
             patch.object(esignature, "formatdate", lambda date: "04/09/2026"):
            state = esignature.apply_signature_state(doc.name, api_payload)
        return state, mails, downloads

    def test_a_pending_callback_only_moves_the_state_and_the_links(self):
        doc = FakeTask()
        state, mails, downloads = self._run(doc, payload(signer_states=("DONE", "NEW")))
        self.assertEqual(state, "Partially signed")
        self.assertEqual(doc.values["fab_fs_signature_status"], "Partially signed")
        self.assertEqual(json.loads(doc.values["fab_fs_signature_links"])["manager"], "https://esign.openapi.com/1")
        self.assertIsNone(doc.values.get("fab_fs_signed_parte"))
        self.assertEqual((mails, downloads), ([], []))

    def test_signing_attaches_both_files_dates_the_task_and_mails_the_office(self):
        doc = FakeTask(fab_fs_end_customer="ROLLS ROYCE MOTOR CARS MILANO")
        state, mails, downloads = self._run(doc, payload("DONE", ("DONE", "DONE")))
        self.assertEqual(state, "Signed")
        self.assertEqual(downloads, ["signedDocument", "audit"])
        self.assertEqual(doc.values["fab_fs_signed_parte"], "/private/A26-015047 signed.pdf")
        self.assertEqual(doc.values["fab_fs_signature_audit"], "/private/A26-015047 audit.pdf")
        self.assertTrue(doc.values["fab_fs_signed_on"])
        self.assertEqual(mails[0]["recipients"], ["office@fabricators.dev"])
        self.assertIn("A26/015047", mails[0]["subject"])
        self.assertEqual(len(mails[0]["attachments"]), 2)
        self.assertEqual(len(doc.comments), 1)

    def test_a_repeated_signed_callback_attaches_and_mails_nothing_more(self):
        doc = FakeTask()
        self._run(doc, payload("DONE", ("DONE", "DONE")))
        signed_on = doc.values["fab_fs_signed_on"]
        state, mails, downloads = self._run(doc, payload("DONE", ("DONE", "DONE")))
        self.assertEqual(state, "Signed")
        self.assertEqual((mails, downloads), ([], []))
        self.assertEqual(doc.values["fab_fs_signed_on"], signed_on)
        self.assertEqual(len(doc.comments), 1)

    def test_a_callback_of_a_replaced_request_is_ignored(self):
        # the parte was sent again after an error, so the old request's callback
        # must not overwrite the live one nor attach the old signed parte
        doc = FakeTask(fab_fs_signature_id="sig-newer")
        state, mails, downloads = self._run(doc, payload("DONE", ("DONE", "DONE")))
        self.assertEqual(state, "stale")
        self.assertEqual((mails, downloads), ([], []))
        self.assertNotIn("fab_fs_signature_status", doc.values)

    def test_without_a_notify_email_the_files_are_still_attached(self):
        doc = FakeTask()
        state, mails, downloads = self._run(doc, payload("DONE", ("DONE", "DONE")), notify_email="")
        self.assertEqual(state, "Signed")
        self.assertEqual(downloads, ["signedDocument", "audit"])
        self.assertEqual(mails, [])


class TestCallback(unittest.TestCase):
    def _call(self, form_dict, secret="shh", task="TASK-2026-00002"):
        applied = []
        stub = frappe_stub(
            form_dict=form_dict,
            PermissionError=PermissionError,
            db=SimpleNamespace(
                exists=lambda dt, name: bool(task),
                get_value=lambda dt, filters, field: task,
            ),
        )
        with patch.object(esignature, "frappe", stub), \
             patch.object(esignature, "get_settings", lambda: settings(callback_secret=secret)), \
             patch.object(esignature, "get_secret", lambda s, f: s.get(f)), \
             patch.object(
                 esignature, "apply_signature_state", lambda t, p: applied.append((t, p)) or "Signed"
             ):
            return esignature.receive_signature_callback(), applied

    def test_the_right_secret_applies_the_payload_to_the_task_it_names(self):
        answer, applied = self._call(
            {"data": payload("DONE"), "custom": {"task": "TASK-2026-00002", "secret": "shh"}}
        )
        self.assertEqual(answer, "Signed")
        self.assertEqual(applied[0][0], "TASK-2026-00002")

    def test_a_wrong_secret_is_refused(self):
        with self.assertRaises(PermissionError):
            self._call({"data": payload(), "custom": {"task": "T", "secret": "guess"}})

    def test_a_callback_without_a_secret_is_refused(self):
        with self.assertRaises(PermissionError):
            self._call({"data": payload(), "custom": {"task": "T"}})

    def test_no_secret_configured_refuses_every_callback(self):
        with self.assertRaises(PermissionError):
            self._call({"data": payload(), "custom": {"secret": ""}}, secret="")

    def test_a_form_encoded_callback_is_read_the_same_way(self):
        answer, applied = self._call(
            {
                "data": json.dumps(payload("DONE")),
                "custom": json.dumps({"task": "TASK-2026-00002", "secret": "shh"}),
            }
        )
        self.assertEqual(answer, "Signed")
        self.assertEqual(applied[0][1]["state"], "DONE")

    def test_an_unknown_request_is_answered_without_touching_anything(self):
        answer, applied = self._call(
            {"data": payload(), "custom": {"secret": "shh"}}, task=None
        )
        self.assertEqual((answer, applied), ("unknown", []))


if __name__ == "__main__":
    unittest.main()
