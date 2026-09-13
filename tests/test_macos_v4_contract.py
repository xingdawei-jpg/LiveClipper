from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "web_client"))

import config
import desktop
import platform_config
from runtime_v4 import desktop_host, launcher, update_service
from runtime_v4 import business_bundle, core_manifest, update_channel


class MacOSV4ContractTests(unittest.TestCase):
    def test_macos_has_a_separate_signed_update_source_root(self) -> None:
        source = ROOT / "release" / "runtime_v4_macos_arm64_update_sources.json"
        document = json.loads(source.read_text(encoding="utf-8"))
        urls = update_service.load_update_source_config(source)
        self.assertEqual(document["runtime_layout_version"], 4)
        self.assertTrue(urls)
        self.assertTrue(all("/v4/macos-arm64/" in url for url in urls))
        self.assertTrue(all("/v4/stable.json" not in url for url in urls))

    def test_macos_core_identity_is_valid_but_not_windows_core_identity(self) -> None:
        identity = "4.0.0-macos-arm64"
        self.assertNotEqual(identity, "4.0.0")
        self.assertTrue(core_manifest.VERSION_PATTERN.fullmatch(identity))
        self.assertTrue(business_bundle.CORE_VERSION_PATTERN.fullmatch(identity))
        self.assertTrue(update_channel.CORE_VERSION_PATTERN.fullmatch(identity))

    def test_macos_business_policy_accepts_only_macos_core(self) -> None:
        policy = json.loads(
            (ROOT / "release" / "runtime_v4_macos_arm64_business_policy.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(policy["compatible_core_versions"], ["4.0.0-macos-arm64"])
        self.assertNotEqual(policy["policy_id"], "liveclipper-business-v1")

    def test_macos_business_bundle_is_signed_for_only_the_macos_core(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private = Ed25519PrivateKey.generate()
            private_path = root / "private.pem"
            public_path = root / "public.pem"
            private_path.write_bytes(private.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ))
            public_path.write_bytes(private.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            ))
            version = json.loads((ROOT / "app" / "version.json").read_text(encoding="utf-8"))["version"]
            archive = root / "mac-business.zip"
            business_bundle.build_business_archive(
                ROOT, archive, application_version=version,
                private_key_path=private_path,
                policy_path=ROOT / "release" / "runtime_v4_macos_arm64_business_policy.json",
            )
            verified = business_bundle.verify_business_archive(
                archive, public_path, expected_version=version,
                expected_core_version="4.0.0-macos-arm64",
            )
            self.assertEqual(verified.compatible_core_versions, ("4.0.0-macos-arm64",))

    def test_macos_data_root_never_uses_appdata(self) -> None:
        with mock.patch.object(config.sys, "platform", "darwin"):
            self.assertEqual(
                config._default_user_data_dir(),
                str(Path.home() / "Library" / "Application Support" / "LiveClipper"),
            )
        with mock.patch.object(launcher.sys, "platform", "darwin"):
            self.assertEqual(
                launcher._data_root(),
                Path.home() / "Library" / "Application Support" / "LiveClipper",
            )

    def test_macos_frozen_ffmpeg_prefers_the_app_frameworks_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = Path(temporary) / "LiveClipper.app"
            executable = app / "Contents" / "MacOS" / "LiveClipper"
            ffmpeg = app / "Contents" / "Frameworks" / "ffmpeg" / "ffmpeg"
            executable.parent.mkdir(parents=True)
            ffmpeg.parent.mkdir(parents=True)
            executable.write_bytes(b"host")
            ffmpeg.write_bytes(b"ffmpeg")
            with (
                mock.patch.object(platform_config, "IS_MAC", True),
                mock.patch.object(platform_config.sys, "frozen", True, create=True),
                mock.patch.object(platform_config.sys, "executable", str(executable)),
                mock.patch.object(platform_config.sys, "_MEIPASS", str(app / "Contents" / "Frameworks"), create=True),
            ):
                directory, command = platform_config._find_ffmpeg()
            self.assertEqual(Path(directory), ffmpeg.parent)
            self.assertEqual(Path(command), ffmpeg)

    def test_macos_host_source_selects_only_macos_channel(self) -> None:
        with mock.patch.object(desktop_host.sys, "platform", "darwin"), mock.patch.object(
            desktop_host.sys, "frozen", False, create=True
        ):
            urls = desktop_host._update_channel_urls()
        self.assertTrue(urls)
        self.assertTrue(all("/v4/macos-arm64/" in url for url in urls))

    def test_macos_restart_reopens_own_app_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = Path(temporary) / "LiveClipper.app"
            launcher_path = app / "Contents" / "MacOS" / "LiveClipper"
            launcher_path.parent.mkdir(parents=True)
            launcher_path.write_bytes(b"launcher")
            root = app / "Contents" / "Resources" / "LiveClipperV4"
            root.mkdir(parents=True)
            layout = desktop_host.HostLayout(root, root / "business", root / "key.pem", "2026.9.14.1")
            with (
                mock.patch.object(desktop_host.sys, "platform", "darwin"),
                mock.patch.object(desktop_host.sys, "frozen", True, create=True),
                mock.patch.object(desktop_host.subprocess, "Popen") as popen,
                mock.patch.object(desktop_host.threading, "Thread") as thread,
            ):
                thread.return_value.start.return_value = None
                self.assertTrue(desktop_host._schedule_launcher_restart(layout))
            self.assertEqual(
                popen.call_args.args[0],
                ["/usr/bin/open", "-n", str(app.resolve())],
            )
            self.assertEqual(popen.call_args.kwargs["cwd"], str(root))

    def test_macos_drag_bridge_dispatches_only_absolute_paths(self) -> None:
        class Event:
            def __init__(self):
                self.added = None

            def __iadd__(self, callback):
                self.added = callback
                return self

            def __isub__(self, _callback):
                return self

        class Document:
            def __init__(self):
                self.events = type("Events", (), {"drop": Event()})()

            def off(self, _event, _callback):
                return None

        document = Document()
        event = document.events.drop
        window = type("Window", (), {"dom": type("Dom", (), {"document": document})()})()
        logs = []
        with mock.patch.object(desktop, "IS_MACOS", True), mock.patch.dict(sys.modules, {"webview.dom": __import__("webview.dom", fromlist=["DOMEventHandler"])}), mock.patch.object(
            desktop, "_dispatch_native_video_drop_after_callback"
        ) as dispatch:
            desktop._enable_macos_native_file_drop_support(window, lambda *row: logs.append(row))
            callback = event.added.callback
            callback({"clientX": 3, "clientY": 4, "dataTransfer": {"files": [
                {"pywebviewFullPath": "/Users/demo/clip.mp4"},
                {"pywebviewFullPath": "/Users/demo/clip.mp4"},
                {"pywebviewFullPath": "relative.mp4"},
            ]}})
        self.assertEqual(dispatch.call_args.args[1]["paths"], ["/Users/demo/clip.mp4"])
        self.assertEqual(dispatch.call_args.args[1]["x"], 3)
        self.assertTrue(any(row[0] == "info" for row in logs))


if __name__ == "__main__":
    unittest.main()
