import importlib.util
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from time import monotonic
import unittest
from unittest.mock import Mock, patch
from urllib.parse import quote, quote_plus

import requests
from wowipy.exceptions import WowiPyException


source_path = Path(__file__).resolve().parents[1] / "app" / "wowiport_client.py"
spec = importlib.util.spec_from_file_location("_wowiport_client_test", source_path)
transport = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transport)


def response(status=200, data=None):
    result = Mock(status_code=status, reason="Testantwort")
    result.json.return_value = data if data is not None else []
    return result


class WowiportClientTests(unittest.TestCase):
    def setUp(self):
        cache_patch = patch("wowipy.rest_adapter.requests_cache.install_cache")
        cache_patch.start()
        self.addCleanup(cache_patch.stop)

    def client(self):
        return transport.WowiportClient(
            hostname="erp.example.invalid", user="test@example.invalid",
            password="a&b=c", api_key="test-key", version="v1.2",
            logger=logging.getLogger(__name__), user_agent="datlaeuft-test",
        )

    def token_response(self):
        return response(data={"access_token": "first-token", "refresh_token": "refresh-token"})

    def test_login_and_all_api_methods_use_timeouts(self):
        with patch.object(transport.requests, "request", side_effect=[
            self.token_response(), *[response(data=[{"Id": 12}]) for _ in range(4)],
        ]) as request:
            client = self.client()
            adapter = client._rest_adapter
            self.assertEqual(adapter.get("CommissioningRead/Commissions").data, [{"Id": 12}])
            adapter.post("CommissioningEdit/Commission", data={"Name": "Test"})
            adapter.put("CommissioningEdit/Commission", data={"Name": "Test"})
            adapter.delete("CommissioningEdit/Commission")
        self.assertEqual(request.call_count, 5)
        self.assertTrue(all(call.kwargs["timeout"] == (10, 60) for call in request.call_args_list))
        self.assertEqual(request.call_args_list[0].kwargs["data"]["password"], "a&b=c")
        self.assertEqual(request.call_args_list[1].kwargs["headers"]["Authorization"], "Bearer first-token")
        self.assertEqual(request.call_args_list[1].kwargs["params"]["apiKey"], "test-key")
        self.assertEqual(client._cache[client.CACHE_PERSONS], [])

    def test_authentication_timeout_fails_during_client_creation(self):
        with patch.object(transport.requests, "request", side_effect=requests.ReadTimeout()):
            with self.assertRaisesRegex(WowiPyException, "Zeitlimit.*oauth2/token"):
                self.client()

    def test_failed_login_includes_the_oauth_error_and_description_in_the_logged_traceback(self):
        logger = logging.getLogger("openwowi-authentication-test")
        configuration = {
            "hostname": "erp.example.invalid", "user": "test@example.invalid", "password": "a&b=c",
            "api_key": "test-key", "user_agent": "datlaeuft-test",
        }
        rejected = response(400, {"error": "invalid_grant", "error_description": "Das Benutzerkonto ist vorübergehend gesperrt."})
        with patch.object(transport.requests, "request", return_value=rejected) as request:
            with self.assertLogs(logger, level="ERROR") as captured:
                try:
                    transport.WowiportClient(**configuration)
                except WowiPyException:
                    logger.exception("process_info_sync_failed process_erp_id=%s", 50695)
                else:
                    self.fail("Die abgewiesene Anmeldung muss einen Fehler auslösen.")
        log = "\n".join(captured.output)
        self.assertIn("process_info_sync_failed process_erp_id=50695", log)
        self.assertIn("Anmeldung fehlgeschlagen (Status 400)", log)
        self.assertIn("error: invalid_grant", log)
        self.assertIn("error_description: Das Benutzerkonto ist vorübergehend gesperrt.", log)
        for secret in ("test@example.invalid", "a&b=c", "test-key"):
            self.assertNotIn(secret, log)
        request.assert_called_once()

    def test_login_error_details_are_available_for_non_400_statuses_and_generic_messages(self):
        for status in (401, 403, 429, 503):
            with self.subTest(status=status), patch.object(transport.requests, "request", return_value=response(
                status, {"error": "temporarily_unavailable", "Message": "Anmeldung derzeit nicht möglich."},
            )) as request:
                with self.assertRaises(WowiPyException) as error:
                    self.client()
                self.assertIn(f"Status {status}", str(error.exception))
                self.assertIn("temporarily_unavailable", str(error.exception))
                self.assertIn("Anmeldung derzeit nicht möglich.", str(error.exception))
                request.assert_called_once()

    def test_rejected_refresh_shows_the_reason_and_does_not_repeat_a_write(self):
        rejected = response(400, {"error": "invalid_grant", "error_description": "Das Aktualisierungstoken ist abgelaufen."})
        with patch.object(transport.requests, "request", side_effect=[self.token_response(), response(401), rejected]) as request:
            with self.assertRaises(WowiPyException) as error:
                self.client()._rest_adapter.post("CommissioningEdit/Commission", data={})
        message = str(error.exception)
        self.assertIn("Token-Erneuerung fehlgeschlagen (Status 400)", message)
        self.assertIn("invalid_grant", message)
        self.assertIn("Das Aktualisierungstoken ist abgelaufen.", message)
        self.assertEqual(request.call_count, 3)
        self.assertEqual(request.call_args.kwargs["data"]["grant_type"], "refresh_token")

    def test_authentication_details_redact_credentials_and_their_encoded_forms(self):
        values = ("test-key", "a&b=c", "test@example.invalid")
        sensitive = {variant for value in values for variant in (value, quote(value, safe=""), quote_plus(value, safe=""))}
        rejected = response(400, {
            "error": "invalid_grant",
            "error_description": "Anmeldung abgelehnt: " + " ".join(sensitive) + "\n\x00\x1b " + "x" * 3000,
            "access_token": "unexpected-access-secret", "refresh_token": ["unexpected-refresh-secret"],
            "password": "unexpected-password-secret", "error_uri": "https://example.invalid/private-error-link",
            "StackTrace": "internal-stack-hidden", "debug": {"Message": "internal-debug-hidden"},
        })
        with patch.object(transport.requests, "request", return_value=rejected):
            with self.assertRaises(WowiPyException) as error:
                self.client()
        message = str(error.exception)
        self.assertIn("invalid_grant", message)
        self.assertIn("Anmeldung abgelehnt", message)
        for value in sensitive | {"unexpected-access-secret", "unexpected-refresh-secret", "unexpected-password-secret",
                                  "private-error-link", "internal-stack-hidden", "internal-debug-hidden", "\n", "\x00", "\x1b"}:
            self.assertNotIn(value, message)
        self.assertLessEqual(len(message.split("OPENWOWI-Details: ", 1)[1]), 2000)

    def test_current_and_explicit_refresh_tokens_are_redacted(self):
        supplied = "request-only-refresh-secret"
        rejected = response(400, {"error": "invalid_grant", "error_description": f"Token abgewiesen: first-token refresh-token {supplied}"})
        with patch.object(transport.requests, "request", side_effect=[self.token_response(), rejected]):
            client = self.client()
            with self.assertRaises(WowiPyException) as error:
                client._rest_adapter._create_token(supplied)
        message = str(error.exception)
        self.assertIn("Token abgewiesen", message)
        for value in ("first-token", "refresh-token", supplied):
            self.assertNotIn(value, message)

    def test_token_assignments_and_authorization_values_inside_messages_are_redacted(self):
        rejected = response(400, {
            "error": "invalid_grant",
            "error_description": 'Authorization: Bearer new-bearer-secret; Basic bmV3LXNlY3JldA==; '
                                 'access_token="new access secret"; refresh_token=new-refresh-secret; '
                                 'client_secret: "new-client-secret"; password="new password secret"; apiKey=new-api-secret',
        })
        with patch.object(transport.requests, "request", return_value=rejected):
            with self.assertRaises(WowiPyException) as error:
                self.client()
        message = str(error.exception)
        self.assertIn("invalid_grant", message)
        for value in ("new-bearer-secret", "bmV3LXNlY3JldA==", "new access secret", "new-refresh-secret",
                      "new-client-secret", "new password secret", "new-api-secret"):
            self.assertNotIn(value, message)

    def test_authentication_plain_text_details_are_normalized_and_html_is_omitted(self):
        for content_type, body, expected in (
            ("text/plain; charset=utf-8", "  invalid_grant:\n  Konto gesperrt. test-key  ", "invalid_grant: Konto gesperrt."),
            ("text/html", "<html>interner Proxyfehler</html>", None),
            ("application/json", '{"error_description": broken', None),
        ):
            rejected = response(400)
            rejected.json.side_effect = ValueError("Ungültige Antwort")
            rejected.headers.get.return_value = content_type
            rejected.text = body
            with self.subTest(content_type=content_type), patch.object(transport.requests, "request", return_value=rejected):
                with self.assertRaises(WowiPyException) as error:
                    self.client()
                message = str(error.exception)
                self.assertIn("Anmeldung fehlgeschlagen (Status 400)", message)
                if expected:
                    self.assertIn(expected, message)
                    self.assertNotIn("test-key", message)
                else:
                    self.assertNotIn("OPENWOWI-Details", message)
                    self.assertNotIn(body, message)

    def test_unusable_authentication_details_do_not_replace_the_status_error_or_expose_token_fields(self):
        for payload in ([], {}, {"error": None, "error_description": None}, {"access_token": "private-token", "password": ["private-password"]}):
            with self.subTest(payload=payload), patch.object(transport.requests, "request", return_value=response(400, payload)):
                with self.assertRaisesRegex(WowiPyException, "Anmeldung fehlgeschlagen.*Status 400") as error:
                    self.client()
                self.assertNotIn("OPENWOWI-Details", str(error.exception))
                self.assertNotIn("private-token", str(error.exception))
                self.assertNotIn("private-password", str(error.exception))

    def test_token_refresh_is_bounded_and_uses_the_new_access_token(self):
        with patch.object(transport.requests, "request", side_effect=[
            self.token_response(), response(401),
            response(data={"access_token": "new-token", "refresh_token": "new-refresh"}),
            response(data=[{"Id": 12}]),
        ]) as request:
            result = self.client()._rest_adapter.get("CommissioningRead/Commissions")
        self.assertEqual(result.data, [{"Id": 12}])
        self.assertEqual(request.call_args_list[2].kwargs["timeout"], (10, 60))
        self.assertEqual(request.call_args_list[2].kwargs["data"]["grant_type"], "refresh_token")
        self.assertEqual(request.call_args_list[3].kwargs["headers"]["Authorization"], "Bearer new-token")

    def test_refresh_timeout_does_not_retry_the_business_request(self):
        with patch.object(transport.requests, "request", side_effect=[
            self.token_response(), response(401), requests.ConnectTimeout(),
        ]) as request:
            with self.assertRaisesRegex(WowiPyException, "Zeitlimit.*oauth2/token"):
                self.client()._rest_adapter.post("CommissioningEdit/Commission", data={})
        self.assertEqual(request.call_count, 3)

    def test_failed_writes_are_not_automatically_replayed(self):
        for failure in (requests.ReadTimeout(), requests.ConnectionError(), response(500)):
            with self.subTest(failure=type(failure).__name__):
                with patch.object(transport.requests, "request", side_effect=[
                    self.token_response(), failure,
                ]) as request:
                    with self.assertRaises(WowiPyException):
                        self.client()._rest_adapter.post("CommissioningEdit/Commission", data={})
                self.assertEqual(request.call_count, 2)

    def test_invalid_json_is_reported_as_a_library_error(self):
        invalid = response()
        invalid.json.side_effect = ValueError("Kein JSON")
        with patch.object(transport.requests, "request", side_effect=[self.token_response(), invalid]):
            with self.assertRaisesRegex(WowiPyException, "Ungültige OPENWOWI-Antwort"):
                self.client()._rest_adapter.get("CommissioningRead/Commissions")

    def test_economic_unit_rejection_includes_validation_details_from_the_actual_post(self):
        rejected = response(400, {"Address": ["Ein Gebäude oder Grundstück muss angegeben werden."]})
        with patch.object(transport.requests, "request", side_effect=[self.token_response(), rejected]) as request:
            with self.assertRaisesRegex(WowiPyException, "Status 400.*Address: Ein Gebäude oder Grundstück"):
                self.client().create_commissioning_commission_wait_for_craftsman_feedback(
                    crafts_process_id=9100, economic_unit_id=1010, commission_type_id=100,
                )
        self.assertEqual(request.call_count, 2)
        self.assertTrue(request.call_args.args[1].endswith("CommissioningEdit/Commission/WaitForCraftsmanFeedback"))
        self.assertEqual(request.call_args.kwargs["json"], {
            "CraftsProcessId": 9100, "CommissionTypeId": 100, "EconomicUnitId": 1010,
        })

    def test_validation_error_formats_preserve_the_field_and_message(self):
        for payload, expected in (
            ({"BuildingId": ["Gebäude fehlt.", "Gebäude fehlt."]}, "BuildingId: Gebäude fehlt."),
            ({"Message": "Angaben prüfen.", "ModelState": {"Address": ["Adresse fehlt."]}}, "Address: Adresse fehlt."),
            ({"title": "Ungültige Angaben", "errors": {"Address": ["Adresse fehlt."]}}, "Address: Adresse fehlt."),
            ({"detail": "Auftragsart ist gesperrt.", "traceId": "trace-hidden"}, "Auftragsart ist gesperrt."),
            (["Erster Fehler.", "Zweiter Fehler."], "Erster Fehler.; Zweiter Fehler."),
            ("Adresse fehlt.", "Adresse fehlt."),
        ):
            with self.subTest(payload=payload), patch.object(
                transport.requests, "request", side_effect=[self.token_response(), response(400, payload)],
            ):
                with self.assertRaises(WowiPyException) as error:
                    self.client()._rest_adapter.post("CommissioningEdit/Commission", data={})
                self.assertIn(expected, str(error.exception))
                self.assertNotIn("trace-hidden", str(error.exception))
                self.assertEqual(str(error.exception).count("BuildingId: Gebäude fehlt."), int("BuildingId" in payload))

    def test_plain_text_validation_errors_are_shown_but_html_errors_are_omitted(self):
        for content_type, text, expected in (
            ("text/plain; charset=utf-8", "  Adresse\n fehlt.  ", "Adresse fehlt."),
            ("text/html", "<html>Proxy-Fehler und interne Details</html>", None),
        ):
            rejected = response(400)
            rejected.json.side_effect = ValueError("Kein JSON")
            rejected.headers.get.return_value = content_type
            rejected.text = text
            with self.subTest(content_type=content_type), patch.object(
                transport.requests, "request", side_effect=[self.token_response(), rejected],
            ):
                with self.assertRaises(WowiPyException) as error:
                    self.client()._rest_adapter.post("CommissioningEdit/Commission", data={})
                message = str(error.exception)
                if expected:
                    self.assertIn(expected, message)
                else:
                    self.assertNotIn("OPENWOWI-Details", message)
                    self.assertNotIn("interne Details", message)

    def test_validation_details_are_bounded_and_do_not_expose_credentials(self):
        rejected = response(400, {
            "Message": "test-key first-token refresh-token a&b=c test@example.invalid " + "x" * 3000,
            "StackTrace": "internal-stack-hidden",
        })
        with patch.object(transport.requests, "request", side_effect=[self.token_response(), rejected]):
            with self.assertRaises(WowiPyException) as error:
                self.client()._rest_adapter.post("CommissioningEdit/Commission", data={})
        message = str(error.exception)
        for value in ("test-key", "first-token", "refresh-token", "a&b=c", "test@example.invalid", "internal-stack-hidden"):
            self.assertNotIn(value, message)
        self.assertLessEqual(len(message.split("OPENWOWI-Details: ", 1)[1]), 2000)

    def test_authentication_and_server_errors_keep_the_response_body_private(self):
        for status in (401, 403, 500):
            with self.subTest(status=status), patch.object(transport.requests, "request", side_effect=[
                self.token_response(), response(status, {"Message": "private-response-body"}),
                self.token_response(), response(status, {"Message": "private-response-body"}),
            ]):
                with self.assertRaises(WowiPyException) as error:
                    self.client()._rest_adapter.post("CommissioningEdit/Commission", data={})
                self.assertNotIn("private-response-body", str(error.exception))

    def test_force_refresh_bypasses_the_http_cache(self):
        with patch.object(transport.requests, "request", side_effect=[self.token_response(), response()]):
            client = self.client()
            with patch.object(transport.requests_cache, "disabled") as disabled:
                client._rest_adapter.get("CommissioningRead/Commissions", force_refresh=True)
        disabled.assert_called_once_with()

    def test_silent_http_server_cannot_block_a_worker_request_indefinitely(self):
        release = Event()

        class SilentHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                release.wait(timeout=2)

        server = ThreadingHTTPServer(("127.0.0.1", 0), SilentHandler)
        server.daemon_threads = True
        thread = Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        thread.start()
        try:
            adapter = transport.WowiportRestAdapter("", "test", "test", "test")
            adapter.url = f"http://127.0.0.1:{server.server_port}/"
            adapter.access_token = "test"
            started_at = monotonic()
            with patch.object(transport, "REQUEST_TIMEOUT", (0.2, 0.05)):
                with self.assertRaisesRegex(WowiPyException, "Zeitlimit"):
                    adapter.get("blocked")
            self.assertLess(monotonic() - started_at, 1)
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=1)


if __name__ == "__main__":
    unittest.main()
