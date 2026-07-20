from __future__ import annotations

import importlib
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "app"
DEPLOY_ROOT = ROOT / "deploy" / "aliyun_fc_license_auth"
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(DEPLOY_ROOT))


license_client = importlib.import_module("license_client")
_backend_spec = importlib.util.spec_from_file_location(
    "_liveclipper_deployed_license_feishu_backend",
    DEPLOY_ROOT / "license_feishu_backend.py",
)
if _backend_spec is None or _backend_spec.loader is None:
    raise ImportError("deployment backend is unavailable")
license_feishu_backend = importlib.util.module_from_spec(_backend_spec)
sys.modules[_backend_spec.name] = license_feishu_backend
_backend_spec.loader.exec_module(license_feishu_backend)


class LicenseDeviceUnbindTests(unittest.TestCase):
    def test_client_keeps_local_activation_when_server_rejects_unbind(self) -> None:
        cache = {"code": "ABCD", "machine_id": "machine-old"}
        server_result = {
            "ok": False,
            "valid": False,
            "retryable": False,
            "error_code": "AUTH_MACHINE_MISMATCH",
            "msg": "current device does not match",
        }

        with mock.patch.object(license_client, "_load_license_code", return_value="ABCD"), mock.patch.object(
            license_client, "_load_cache", return_value=cache
        ), mock.patch.object(license_client, "_get_machine_id", return_value="machine-old"), mock.patch.object(
            license_client, "_fc_unbind", return_value=server_result
        ) as fc_unbind, mock.patch.object(license_client, "_record_license_event"), mock.patch.object(
            license_client, "_save_cache"
        ) as save_cache, mock.patch.object(license_client, "_set_activation_cache") as set_activation_cache:
            result = license_client.deactivate_device()

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "AUTH_MACHINE_MISMATCH")
        self.assertTrue(result["msg"])
        fc_unbind.assert_called_once_with("ABCD", "machine-old")
        save_cache.assert_not_called()
        set_activation_cache.assert_not_called()

    def test_client_clears_local_activation_after_server_confirms_unbind(self) -> None:
        cache = {"code": "ABCD", "machine_id": "machine-old"}

        with mock.patch.object(license_client, "_load_license_code", return_value="ABCD"), mock.patch.object(
            license_client, "_load_cache", return_value=cache
        ), mock.patch.object(license_client, "_get_machine_id", return_value="machine-old"), mock.patch.object(
            license_client, "_fc_unbind", return_value={"ok": True, "retryable": False}
        ) as fc_unbind, mock.patch.object(license_client, "_record_license_event"), mock.patch.object(
            license_client, "_save_cache"
        ) as save_cache, mock.patch.object(license_client, "_set_activation_cache") as set_activation_cache, mock.patch.object(
            license_client.os.path, "exists", return_value=False
        ):
            result = license_client.deactivate_device()

        self.assertTrue(result["ok"])
        fc_unbind.assert_called_once_with("ABCD", "machine-old")
        save_cache.assert_called_once_with({"trial_uses_left": 0, "previously_activated": True})
        set_activation_cache.assert_called_once()

    def test_rebinding_keeps_the_existing_expiry_after_unbind(self) -> None:
        now = 1_800_000_000
        expires_at = now + 12 * 86400
        activated_at = now - 20 * 86400
        fields = {
            license_feishu_backend.FIELD_STATUS: license_feishu_backend.STATUS_ACTIVE,
            license_feishu_backend.FIELD_DEVICE_ID: "machine-old",
            license_feishu_backend.FIELD_DEVICE_INFO: "old device",
            license_feishu_backend.FIELD_EXPIRES_AT: expires_at * 1000,
            license_feishu_backend.FIELD_ACTIVATED_AT: activated_at * 1000,
        }
        writes: list[dict[str, object]] = []

        def update_record(_record_id: str, updates: dict[str, object]) -> None:
            writes.append(dict(updates))
            fields.update(updates)

        with mock.patch.object(license_feishu_backend, "_find_record", return_value=("rec-1", fields)), mock.patch.object(
            license_feishu_backend, "_update_record", side_effect=update_record
        ), mock.patch.object(license_feishu_backend.time, "time", return_value=now):
            unbind_result = license_feishu_backend.unbind("01ABCD", "machine-old")
            activate_result = license_feishu_backend.activate("01ABCD", "machine-new")

        self.assertTrue(unbind_result["ok"])
        self.assertTrue(activate_result["ok"])
        self.assertEqual(fields[license_feishu_backend.FIELD_STATUS], license_feishu_backend.STATUS_ACTIVE)
        self.assertEqual(fields[license_feishu_backend.FIELD_DEVICE_ID], "machine-new")
        self.assertEqual(fields[license_feishu_backend.FIELD_EXPIRES_AT], expires_at * 1000)
        self.assertEqual(fields[license_feishu_backend.FIELD_ACTIVATED_AT], activated_at * 1000)
        self.assertEqual(activate_result["expires_at"], expires_at)
        self.assertEqual(len(writes), 2)


if __name__ == "__main__":
    unittest.main()
