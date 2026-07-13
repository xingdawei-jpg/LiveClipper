from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, path: Path):
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CacheCleanupTests(unittest.TestCase):
    def test_server_targets_regenerable_chrome_data_but_not_login_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            appdata = base / "Roaming"
            localappdata = base / "Local"
            env = os.environ.copy()
            env["APPDATA"] = str(appdata)
            env["LOCALAPPDATA"] = str(localappdata)
            command = [
                sys.executable,
                "-c",
                (
                    "import json,sys;"
                    f"sys.path.insert(0,{str(ROOT / 'app')!r});"
                    f"sys.path.insert(0,{str(ROOT / 'web_client')!r});"
                    "import server;"
                    "print(json.dumps({'targets':[str(p) for p in server._cache_clear_targets()],"
                    "'skip':sorted(server._USER_DATA_MIGRATE_SKIP)}))"
                ),
            ]
            completed = subprocess.run(
                command,
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=45,
                check=True,
            )
            payload = json.loads(completed.stdout.strip().splitlines()[-1])
            targets = {os.path.normcase(os.path.normpath(path)) for path in payload["targets"]}
            profile = appdata / "LiveClipper" / "chrome-live-poc"

            for relative in (
                "OptGuideOnDeviceModel",
                "OptGuideOnDeviceClassifierModel",
                "Default/Cache",
                "Default/Code Cache",
                "Default/GPUCache",
            ):
                self.assertIn(
                    os.path.normcase(os.path.normpath(profile / relative)),
                    targets,
                )
            for relative in (
                "Default/Network",
                "Default/Local Storage",
                "Default/IndexedDB",
                "Default/Sessions",
                "Default/Extensions",
            ):
                self.assertNotIn(
                    os.path.normcase(os.path.normpath(profile / relative)),
                    targets,
                )
            self.assertIn(
                os.path.normcase(
                    os.path.normpath(localappdata / "LiveClipper" / "update_backups")
                ),
                targets,
            )
            self.assertIn("chrome-live-poc", payload["skip"])
            self.assertIn("live_rec_diagnostics", payload["skip"])

    def test_chrome_launch_caps_cache_and_disables_local_models(self) -> None:
        module = _load_module(
            "liveclipper_test_douyin_chrome",
            ROOT / "web_client" / "tools" / "douyin_chrome_live_poc.py",
        )
        with tempfile.TemporaryDirectory() as temp:
            with (
                patch.object(module, "find_chrome", return_value=r"C:\Chrome\chrome.exe"),
                patch.object(module.subprocess, "Popen") as popen,
            ):
                module.launch_chrome(9222, "https://live.douyin.com/", Path(temp))
            command = popen.call_args.args[0]

        self.assertIn("--disk-cache-size=134217728", command)
        self.assertIn("--media-cache-size=67108864", command)
        feature_switch = next(
            value for value in command if value.startswith("--disable-features=")
        )
        self.assertIn("OptimizationGuideOnDeviceModel", feature_switch)
        self.assertIn("OptimizationGuideModelExecution", feature_switch)
        self.assertIn("TextSafetyClassifier", feature_switch)
        self.assertNotIn("--disable-component-update", command)

    def test_update_backup_pruning_keeps_two_newest_transactions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            localappdata = Path(temp)
            backup_root = localappdata / "LiveClipper" / "update_backups"
            backup_root.mkdir(parents=True)
            for index, name in enumerate(("old", "middle", "new"), start=1):
                path = backup_root / name
                path.mkdir()
                (path / "transaction.json").write_text("{}", encoding="utf-8")
                os.utime(path, (index, index))
            env = os.environ.copy()
            env["APPDATA"] = str(Path(temp) / "Roaming")
            env["LOCALAPPDATA"] = str(localappdata)
            command = [
                sys.executable,
                "-c",
                (
                    f"import sys;sys.path.insert(0,{str(ROOT / 'app')!r});"
                    f"sys.path.insert(0,{str(ROOT / 'web_client')!r});"
                    "import server;server._prune_local_update_backups(keep=2)"
                ),
            ]
            subprocess.run(command, cwd=ROOT, env=env, timeout=45, check=True)

            self.assertFalse((backup_root / "old").exists())
            self.assertTrue((backup_root / "middle").is_dir())
            self.assertTrue((backup_root / "new").is_dir())


if __name__ == "__main__":
    unittest.main()