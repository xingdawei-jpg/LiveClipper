from __future__ import annotations

import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "web_client"))

server = importlib.import_module("server")


class LiveProbeIntegrityTests(unittest.TestCase):
    def test_text_hash_is_stable_across_windows_line_endings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lf_script = root / "probe_lf.py"
            crlf_script = root / "probe_crlf.py"
            lf_script.write_bytes(b"print('one')\nprint('two')\n")
            crlf_script.write_bytes(b"print('one')\r\nprint('two')\r\n")

            self.assertEqual(
                server._short_file_hash(lf_script, 64),
                server._short_file_hash(crlf_script, 64),
            )

    def test_probe_validation_accepts_packaged_crlf_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / "douyin_active_product_probe_poc.py"
            script.write_bytes(b"print('probe')\r\n")
            expected = server._short_file_hash(script, 64)

            with mock.patch.object(server, "_manifest_expected_hash", return_value=expected):
                server._validate_active_probe_script(script)

    def test_probe_validation_still_rejects_content_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / "douyin_active_product_probe_poc.py"
            script.write_bytes(b"print('changed')\r\n")

            with mock.patch.object(server, "_manifest_expected_hash", return_value="0" * 64):
                with self.assertRaisesRegex(RuntimeError, "直播商品探针版本不一致"):
                    server._validate_active_probe_script(script)


if __name__ == "__main__":
    unittest.main()
