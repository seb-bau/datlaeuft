import importlib.util
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from time import monotonic
import unittest
from unittest.mock import Mock, patch

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
