import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "web_client"))
import server


class ConnectionProbeTests(unittest.TestCase):
    def call(self, response=None, error=None, **overrides):
        values = dict(api_key="test-secret", base_url="https://ark.cn-beijing.volces.com/api/v3", model="deepseek-v3")
        values.update(overrides)
        resp = MagicMock()
        resp.status = 200
        resp.read.return_value = json.dumps(response if response is not None else {"choices": [{"message": {"content": "OK"}}]}).encode()
        resp.__enter__.return_value = resp
        with patch.object(server, "_load_settings", return_value={}), patch.object(server, "emit_log"), patch.object(server.urllib.request, "urlopen", return_value=resp, side_effect=error) as request:
            result = server.test_ai(server.SettingsPayload(**values))
        return result, request

    def test_ark_hosted_deepseek_uses_actual_chat(self):
        result, request = self.call()
        self.assertTrue(result["ok"])
        req = request.call_args.args[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertTrue(req.full_url.endswith("/api/v3/chat/completions"))
        self.assertEqual(json.loads(req.data)["model"], "deepseek-v3")

    def test_empty_model_and_url_rejected_without_request(self):
        for values in ({"model": ""}, {"base_url": ""}):
            result, request = self.call(**values)
            self.assertFalse(result["ok"])
            request.assert_not_called()

    def test_success_status_with_wrong_response_is_not_pass(self):
        result, _ = self.call(response={"data": []})
        self.assertFalse(result["ok"])

    def test_error_preserves_provider_detail_without_key(self):
        error = urllib.error.HTTPError("https://example.test", 404, "missing", {}, io.BytesIO(b'model missing test-secret'))
        result, _ = self.call(error=error)
        self.assertFalse(result["ok"])
        self.assertIn("model missing", result["message"])
        self.assertNotIn("test-secret", result["message"])
        self.assertNotIn("https://api.deepseek.com", result["message"])

    def test_custom_endpoint_is_preserved(self):
        result, request = self.call(base_url="https://example.test/v1", model="custom-model")
        self.assertTrue(result["ok"])
        self.assertEqual(request.call_args.args[0].full_url, "https://example.test/v1/chat/completions")
