from __future__ import annotations

import importlib
import io
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

license_client = importlib.import_module("license_client")
license_guard = importlib.import_module("license_guard")
license_token = importlib.import_module("license_token")


class LicenseTimeoutFailOpenTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_result = license_client._last_activation_result
        self._old_time = license_client._last_activation_time
        self._old_refreshing = license_client._activation_refreshing

    def tearDown(self) -> None:
        license_client._last_activation_result = self._old_result
        license_client._last_activation_time = self._old_time
        license_client._activation_refreshing = self._old_refreshing

    def test_feature_gate_uses_cached_result_without_sync_verify(self) -> None:
        local = {"activated": True, "token_verified": True}
        with mock.patch.object(license_client, "check_activation_cached", return_value=local) as cached, mock.patch.object(
            license_client, "check_activation", side_effect=AssertionError("feature gate must not verify synchronously")
        ) as verify:
            results = [license_guard.get_feature_access() for _ in range(3)]

        self.assertTrue(all(result["ok"] for result in results))
        self.assertEqual(cached.call_count, 3)
        verify.assert_not_called()

    def test_http_500_timeout_is_retryable_and_keeps_valid_token(self) -> None:
        error = urllib.error.HTTPError(
            "https://example.invalid/api/verify",
            500,
            "Internal Server Error",
            None,
            io.BytesIO(b'{"ok":false,"valid":false,"msg":"server error: The read operation timed out"}'),
        )
        remote = license_client._remote_http_error_result(error)
        self.assertTrue(remote["retryable"])
        self.assertEqual(remote["error_code"], "AUTH_UPSTREAM_ERROR")

        token_result = {"activated": True, "token_verified": True}
        with mock.patch.object(license_client, "_load_cache", return_value={"code": "ABCD"}), mock.patch.object(
            license_client, "_token_activation_result", return_value=token_result
        ), mock.patch.object(license_client, "_refresh_token_from_saved_code", return_value=remote):
            access = license_client.check_activation()

        self.assertTrue(access["activated"])
        self.assertIn("online_warning", access)

    def test_network_failure_is_retryable_without_need_activate(self) -> None:
        with mock.patch.object(license_client, "_verify_online", return_value=None):
            result = license_client._refresh_token_from_saved_code({"code": "ABCD"})
        self.assertTrue(result["retryable"])
        self.assertNotIn("need_activate", result)

    def test_authoritative_rejections_block_and_revocation_marks_local_state(self) -> None:
        cases = (
            ({"ok": False, "valid": False, "revoked": True, "msg": "revoked"}, True),
            ({"ok": False, "valid": False, "error_code": "AUTH_EXPIRED", "msg": "expired"}, False),
            ({"ok": False, "valid": False, "error_code": "AUTH_MACHINE_MISMATCH", "msg": "machine mismatch"}, False),
        )
        for response, revoked in cases:
            with self.subTest(response=response), mock.patch.object(license_client, "_verify_online", return_value=response), mock.patch.object(
                license_client, "_write_revoked_marker"
            ) as write_marker, mock.patch.object(license_client, "_remember_authoritative_rejection"):
                result = license_client._refresh_token_from_saved_code({"code": "ABCD"})

            self.assertTrue(result["need_activate"])
            self.assertNotIn("retryable", result)
            self.assertEqual(write_marker.called, revoked)

    def test_offline_window_expiry_and_no_token_require_online_verification(self) -> None:
        with mock.patch.object(
            license_token, "verify_license_token", return_value={"ok": False, "reason": "license token offline window expired"}
        ):
            expired = license_client._token_activation_result({"license_token": "signed-token"})
        self.assertTrue(expired["need_activate"])

        with mock.patch.object(license_client, "_check_revoked_marker", return_value=False), mock.patch.object(
            license_client, "_load_cache", return_value={}
        ), mock.patch.object(license_client, "_refresh_token_from_saved_code") as refresh:
            missing = license_client._check_activation_local_fast()
        self.assertTrue(missing["need_activate"])
        refresh.assert_not_called()

    def test_background_refresh_is_single_flight(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def delayed_check():
            entered.set()
            release.wait(timeout=2)
            return {"activated": True}

        license_client._activation_refreshing = False
        with mock.patch.object(license_client, "check_activation", side_effect=delayed_check) as check:
            self.assertTrue(license_client.refresh_activation_cache_async())
            self.assertTrue(entered.wait(timeout=1))
            self.assertFalse(license_client.refresh_activation_cache_async())
            release.set()
            for _ in range(20):
                if not license_client._activation_refreshing:
                    break
                threading.Event().wait(0.02)

        self.assertEqual(check.call_count, 1)

    def test_fc_copies_match_and_timeout_payload_is_structured(self) -> None:
        deploy = ROOT / "deploy" / "aliyun_fc_license_auth"
        for filename in ("license_server.py", "license_feishu_backend.py", "license_token.py", "license_stats_store.py"):
            self.assertEqual((deploy / filename).read_bytes(), (deploy / "app" / filename).read_bytes(), filename)

        sys.path.insert(0, str(deploy))
        try:
            spec = importlib.util.spec_from_file_location("fc_license_server_timeout_test", deploy / "license_server.py")
            module = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(module)
        finally:
            sys.path.pop(0)

        class TimeoutErrorForTest(Exception):
            retryable = True
            error_code = "AUTH_UPSTREAM_TIMEOUT"

        payload = module._auth_upstream_error_payload(TimeoutErrorForTest())
        self.assertEqual(payload, {
            "ok": False,
            "valid": False,
            "retryable": True,
            "error_code": "AUTH_UPSTREAM_TIMEOUT",
            "msg": "\u6388\u6743\u670d\u52a1\u6682\u65f6\u7e41\u5fd9",
        })
        self.assertIsNotNone(module.ThreadingHTTPServer)


    def test_http_429_is_retryable(self) -> None:
        error = urllib.error.HTTPError(
            "https://example.invalid/api/verify",
            429,
            "Too Many Requests",
            None,
            io.BytesIO(b'{}'),
        )
        result = license_client._remote_http_error_result(error)
        self.assertTrue(result["retryable"])
        self.assertEqual(result["error_code"], "AUTH_UPSTREAM_RATE_LIMIT")

    def test_fc_timeout_retry_concurrency_limit_and_503_handler(self) -> None:
        deploy = ROOT / "deploy" / "aliyun_fc_license_auth"
        backend_spec = importlib.util.spec_from_file_location(
            "fc_license_backend_timeout_test", deploy / "app" / "license_feishu_backend.py"
        )
        backend = importlib.util.module_from_spec(backend_spec)
        assert backend_spec.loader is not None
        backend_spec.loader.exec_module(backend)

        response = mock.MagicMock()
        response.read.return_value = b'{"code": 0, "data": {}}'
        response_context = mock.MagicMock()
        response_context.__enter__.return_value = response
        request = urllib.request.Request("https://example.invalid/bitable")
        with mock.patch.object(
            backend.urllib.request, "urlopen", side_effect=[TimeoutError("The read operation timed out"), response_context]
        ) as urlopen, mock.patch.object(backend.time, "sleep"):
            payload = backend._request_json(request, timeout=0.01, retries=1)
        self.assertEqual(payload["code"], 0)
        self.assertEqual(urlopen.call_count, 2)

        original_slots = backend._request_slots
        slots = threading.BoundedSemaphore(1)
        self.assertTrue(slots.acquire())
        backend._request_slots = slots
        try:
            with mock.patch.object(backend, "_FEISHU_QUEUE_TIMEOUT_SECONDS", 0.001), self.assertRaises(
                backend.AuthUpstreamError
            ) as raised:
                backend._request_json(request, timeout=0.01, retries=0)
        finally:
            slots.release()
            backend._request_slots = original_slots
        self.assertEqual(raised.exception.error_code, "AUTH_UPSTREAM_BUSY")

        sys.path.insert(0, str(deploy))
        try:
            server_spec = importlib.util.spec_from_file_location("fc_license_server_handler_test", deploy / "license_server.py")
            server = importlib.util.module_from_spec(server_spec)
            assert server_spec.loader is not None
            server_spec.loader.exec_module(server)
        finally:
            sys.path.pop(0)

        class RetryableReadTimeout(Exception):
            retryable = True
            error_code = "AUTH_UPSTREAM_TIMEOUT"

        handler = object.__new__(server.LicenseHandler)
        handler._path = lambda: "/api/verify"
        handler._query = lambda: {"code": "ABCD", "machine_id": "machine-1"}
        sent = []
        handler._send_json = lambda payload, status=200: sent.append((payload, status))
        with mock.patch.object(server, "verify_license", side_effect=RetryableReadTimeout()):
            handler.do_GET()
        self.assertEqual(sent, [(
            {
                "ok": False,
                "valid": False,
                "retryable": True,
                "error_code": "AUTH_UPSTREAM_TIMEOUT",
                "msg": "\u6388\u6743\u670d\u52a1\u6682\u65f6\u7e41\u5fd9",
            },
            503,
        )])


    def test_retryable_warning_logs_without_denial(self) -> None:
        logs = []
        access = {
            "ok": True,
            "activated": True,
            "trial": False,
            "raw": {"online_warning": "authorization service busy"},
        }
        with mock.patch.object(license_guard, "get_feature_access", return_value=access), mock.patch.object(
            license_guard, "_record_event"
        ):
            allowed = license_guard.require_feature_access(
                "smart_clip",
                log_fn=lambda message, level: logs.append((message, level)),
                show_dialog=False,
            )

        self.assertTrue(allowed)
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0][1], "warn")


    def test_retryable_json_response_does_not_store_token(self) -> None:
        response = mock.MagicMock()
        response.read.return_value = b'{"ok":false,"retryable":true,"license_token":"do-not-save"}'
        context = mock.MagicMock()
        context.__enter__.return_value = response
        with mock.patch.object(license_client, "_get_fingerprints", return_value={}), mock.patch.object(
            license_client.urllib.request, "urlopen", return_value=context
        ), mock.patch.object(license_client, "_store_license_token_from_response") as store:
            result = license_client._verify_online("ABCD", "machine-1")

        self.assertTrue(result["retryable"])
        store.assert_not_called()


if __name__ == "__main__":
    unittest.main()
