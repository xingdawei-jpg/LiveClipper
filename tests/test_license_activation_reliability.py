from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))


license_client = importlib.import_module("license_client")
try:
    license_feishu_backend = importlib.import_module("license_feishu_backend")
except ModuleNotFoundError:
    license_feishu_backend = None


class LicenseActivationReliabilityTests(unittest.TestCase):
    def test_verify_uses_extended_timeout(self) -> None:
        response = mock.MagicMock()
        response.read.return_value = b'{"ok": false, "valid": false, "msg": "not found"}'
        context = mock.MagicMock()
        context.__enter__.return_value = response

        with mock.patch.object(license_client, "_get_fingerprints", return_value={}), mock.patch.object(
            license_client.urllib.request, "urlopen", return_value=context
        ) as urlopen:
            result = license_client._verify_online("ABCD", "machine-1")

        self.assertFalse(result["ok"])
        self.assertEqual(
            urlopen.call_args.kwargs["timeout"],
            license_client._VERIFY_REQUEST_TIMEOUT_SECONDS,
        )

    def test_activate_uses_extended_timeout(self) -> None:
        response = mock.MagicMock()
        response.read.return_value = b'{"ok": false, "valid": false, "msg": "not found"}'
        context = mock.MagicMock()
        context.__enter__.return_value = response

        with mock.patch.object(license_client, "_get_fingerprints", return_value={}), mock.patch.object(
            license_client, "_get_device_info", return_value={}
        ), mock.patch.object(license_client.urllib.request, "urlopen", return_value=context) as urlopen:
            result = license_client._fc_activate("ABCD", "machine-1", {}, 1_800_000_000)

        self.assertFalse(result["ok"])
        self.assertEqual(
            urlopen.call_args.kwargs["timeout"],
            license_client._ACTIVATE_REQUEST_TIMEOUT_SECONDS,
        )

    @unittest.skipUnless(license_feishu_backend is not None, "deployment backend module is unavailable")
    def test_recent_successful_verify_skips_background_bitable_write(self) -> None:
        now = 1_800_000_000
        fields = {
            "状态": "已激活",
            "设备ID": "machine-1",
            "到期日期": (now + 86400) * 1000,
            "最后联网验证时间": (now - 60) * 1000,
            "最后验证结果": "通过",
        }

        with mock.patch.object(license_feishu_backend, "_select_record", return_value=("rec-1", fields)), mock.patch.object(
            license_feishu_backend.time, "time", return_value=now
        ), mock.patch.object(license_feishu_backend, "_background") as background:
            result = license_feishu_backend.verify("ABCD", "machine-1")

        self.assertTrue(result["ok"])
        background.assert_not_called()

    @unittest.skipUnless(license_feishu_backend is not None, "deployment backend module is unavailable")
    def test_stale_successful_verify_keeps_a_periodic_bitable_write(self) -> None:
        now = 1_800_000_000
        fields = {
            "状态": "已激活",
            "设备ID": "machine-1",
            "到期日期": (now + 86400) * 1000,
            "最后联网验证时间": (
                now - license_feishu_backend._VERIFY_WRITE_INTERVAL_SECONDS - 1
            )
            * 1000,
            "最后验证结果": "通过",
        }

        with mock.patch.object(license_feishu_backend, "_select_record", return_value=("rec-1", fields)), mock.patch.object(
            license_feishu_backend.time, "time", return_value=now
        ), mock.patch.object(license_feishu_backend, "_background") as background:
            result = license_feishu_backend.verify("ABCD", "machine-1")

        self.assertTrue(result["ok"])
        background.assert_called_once()


if __name__ == "__main__":
    unittest.main()
