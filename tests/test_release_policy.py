from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
APP = ROOT / "app"
for path in (TOOLS, APP):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def load_tool(name: str):
    path = TOOLS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"release_policy_{name}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ReleasePolicyTests(unittest.TestCase):
    def test_machine_policy_matches_actual_distribution_decision(self) -> None:
        policy = json.loads(
            (ROOT / "release" / "release_policy.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            policy["version_sources"],
            {
                "runtime": "app/version.json",
                "stable_components": "tools/runtime_v3_versions.py",
                "full_baselines": "release/baselines.json",
                "live_update_channel": "release/stable.json",
            },
        )
        self.assertEqual(
            policy["distribution"]["full_package"]["provider"],
            "baidu-netdisk",
        )
        self.assertFalse(
            policy["distribution"]["full_package"]["publish_to_github_releases"]
        )
        self.assertEqual(
            policy["distribution"]["full_package"]["share_url_hosts"],
            ["pan.baidu.com"],
        )
        self.assertEqual(
            policy["distribution"]["automatic_patch"]["manifest_url_hosts"],
            ["github.com"],
        )
        self.assertEqual(
            policy["distribution"]["automatic_patch"]["minimum_sources"],
            1,
        )
        self.assertFalse(
            policy["release_types"]["business_runtime"]["new_full_package_required"]
        )
        self.assertTrue(
            policy["release_types"]["business_runtime"]["full_package_forbidden"]
        )
        self.assertTrue(
            policy["release_types"]["business_runtime"]["signed_patch_required"]
        )
        self.assertTrue(
            policy["release_types"]["full_baseline"]["new_full_package_required"]
        )
        self.assertNotIn("baidu_download_hash", policy["acceptance_gates"]["always"])
        self.assertNotIn(
            "baidu_download_hash",
            policy["acceptance_gates"]["business_runtime"],
        )
        self.assertIn(
            "baidu_download_hash",
            policy["acceptance_gates"]["full_baseline"],
        )

    def test_current_baseline_is_registered_with_exact_stable_versions(self) -> None:
        registry = json.loads(
            (ROOT / "release" / "baselines.json").read_text(encoding="utf-8")
        )
        current = [
            item
            for item in registry["baselines"]
            if item.get("status") == "current"
        ]
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["version"], registry["current_baseline_version"])
        self.assertEqual(current[0]["version"], "2026.7.15.1")
        self.assertEqual(current[0]["launcher_version"], "1.1.0")
        self.assertEqual(current[0]["updater_version"], "1.3.0")
        self.assertEqual(
            current[0]["package"]["sha256"],
            "3F8BB2DB275D73E2192B3F84C03717FBFE2485BD6DFA70439A1CF957898D77C4",
        )

    def test_channel_builder_enforces_baidu_full_and_github_patch_policy(self) -> None:
        builder = load_tool("build_release_channel")
        policy = builder._release_policy()
        github_patch = {
            "filename": "LiveClipperPatch_1_to_2_v3.zip",
            "sources": [
                {
                    "name": "GitHub",
                    "url": "https://github.com/example/repo/releases/download/v2/patch.zip",
                }
            ],
        }
        builder._validate_distribution_policy(
            "https://pan.baidu.com/s/example",
            [github_patch],
            policy,
        )
        with self.assertRaisesRegex(ValueError, "not GitHub Releases"):
            builder._validate_distribution_policy(
                "https://github.com/example/repo/releases/download/v2/full.zip",
                [github_patch],
                policy,
            )
        oss_patch = {
            "filename": "LiveClipperPatch_1_to_2_v3.zip",
            "sources": [
                {
                    "name": "OSS",
                    "url": "https://bucket.oss-cn-hangzhou.aliyuncs.com/patch.zip",
                }
            ],
        }
        with self.assertRaisesRegex(ValueError, "not allowed"):
            builder._validate_distribution_policy("", [oss_patch], policy)

    def test_channel_builder_separates_business_patch_and_full_baseline(self) -> None:
        builder = load_tool("build_release_channel")
        self.assertEqual(builder._resolve_release_type("", 1), "business_runtime")
        self.assertEqual(builder._resolve_release_type("", 0), "full_baseline")
        builder._validate_release_shape(
            "business_runtime",
            package_present=False,
            patch_count=1,
        )
        with self.assertRaisesRegex(ValueError, "must not include a full package"):
            builder._validate_release_shape(
                "business_runtime",
                package_present=True,
                patch_count=1,
            )
        with self.assertRaisesRegex(ValueError, "at least one signed patch"):
            builder._validate_release_shape(
                "business_runtime",
                package_present=False,
                patch_count=0,
            )
        builder._validate_release_shape(
            "full_baseline",
            package_present=True,
            patch_count=0,
        )
        with self.assertRaisesRegex(ValueError, "requires a full package"):
            builder._validate_release_shape(
                "full_baseline",
                package_present=False,
                patch_count=0,
            )

    def test_development_preflight_accepts_registered_split_state(self) -> None:
        preflight = load_tool("release_preflight")
        baseline = json.loads(
            (ROOT / "release" / "baselines.json").read_text(encoding="utf-8")
        )["baselines"][-1]
        runtime_version = json.loads(
            (ROOT / "app" / "version.json").read_text(encoding="utf-8-sig")
        )["version"]
        expected_hash = baseline["package"]["sha256"]
        with (
            mock.patch.object(preflight, "sha256_file", return_value=expected_hash),
            mock.patch.object(preflight, "inspect_full_zip"),
            mock.patch.object(preflight, "git_paths"),
        ):
            report = preflight.run_preflight("development")
        self.assertFalse(report.errors)
        self.assertEqual(report.facts["runtime_version"], runtime_version)
        self.assertEqual(report.facts["channel_version"], "2026.7.14.7")
        self.assertTrue(
            any("ahead of live channel" in item for item in report.warnings)
        )
        self.assertTrue(
            any("legacy non-policy host" in item for item in report.warnings)
        )

    def test_candidate_preflight_rejects_ready_status(self) -> None:
        preflight = load_tool("release_preflight")
        stable = json.loads(
            (ROOT / "release" / "stable.json").read_text(encoding="utf-8-sig")
        )
        with tempfile.TemporaryDirectory(
            dir=ROOT / "release" / "candidates"
        ) as temp:
            manifest = Path(temp) / "stable.hold.json"
            manifest.write_text(
                json.dumps(stable, ensure_ascii=False),
                encoding="utf-8",
            )
            with (
                mock.patch.object(preflight, "verify_signed"),
                mock.patch.object(preflight, "sha256_file", return_value="0" * 64),
                mock.patch.object(preflight, "inspect_full_zip"),
                mock.patch.object(preflight, "git_paths"),
            ):
                report = preflight.run_preflight(
                    "candidate",
                    manifest_path=manifest,
                )
        self.assertTrue(
            any("candidate channel must be hold" in item for item in report.errors)
        )

    def test_full_package_preflight_requires_bundled_media_tools(self) -> None:
        preflight = load_tool("release_preflight")
        version = "2026.7.15.2"
        with tempfile.TemporaryDirectory() as temp:
            package = Path(temp) / "full.zip"
            root = "LiveClipperWeb"
            with zipfile.ZipFile(package, "w") as archive:
                archive.writestr(
                    f"{root}/current.json",
                    json.dumps({"current_version": version}),
                )
                archive.writestr(
                    f"{root}/install_manifest.json",
                    json.dumps(
                        {
                            "launcher_version": "1.1.0",
                            "updater_version": "1.3.0",
                        }
                    ),
                )
                archive.writestr(
                    f"{root}/versions/{version}/runtime_manifest.json",
                    json.dumps({"version": version}),
                )
                for relative in (
                    "LiveClipperWeb.exe",
                    "updater/LiveClipperUpdater.exe",
                    "updater/release_update_public_key.pem",
                    f"versions/{version}/_internal/webview2_runtime/msedgewebview2.exe",
                ):
                    archive.writestr(f"{root}/{relative}", b"fixture")
            report = preflight.Report("candidate")
            with mock.patch.object(preflight, "verify_signed"):
                preflight.inspect_full_zip(
                    report,
                    package,
                    version,
                    "1.1.0",
                    "1.3.0",
                    full_test=True,
                )
        self.assertTrue(
            any("bundled FFmpeg tools are missing" in item for item in report.errors)
        )

    def test_channel_builder_cannot_generate_ready_directly(self) -> None:
        source = (TOOLS / "build_release_channel.py").read_text(encoding="utf-8")
        self.assertIn(
            'choices=("hold", "awaiting-external-distribution")',
            source,
        )
        self.assertNotIn('choices=("ready",', source)
        self.assertTrue((TOOLS / "promote_release_channel.py").is_file())

    def test_promotion_acceptance_is_bound_to_exact_candidate_hash(self) -> None:
        promoter = load_tool("promote_release_channel")
        policy = json.loads(
            (ROOT / "release" / "release_policy.json").read_text(encoding="utf-8")
        )
        required = [
            *policy["acceptance_gates"]["always"],
            *policy["acceptance_gates"]["business_runtime"],
        ]
        with tempfile.TemporaryDirectory() as temp:
            candidate_path = Path(temp) / "stable.hold.json"
            candidate_path.write_text('{"channel_status":"hold"}', encoding="utf-8")
            acceptance = {
                "version": "2026.7.15.2",
                "candidate_sha256": promoter.sha256_file(candidate_path).upper(),
                "release_type": "business_runtime",
                "gates": {name: "pass" for name in required},
                "evidence": {name: {"result": "verified"} for name in required},
            }
            promoter.validate_acceptance(
                candidate_path,
                {"version": "2026.7.15.2"},
                acceptance,
                policy,
            )
            acceptance["candidate_sha256"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "exact candidate"):
                promoter.validate_acceptance(
                    candidate_path,
                    {"version": "2026.7.15.2"},
                    acceptance,
                    policy,
                )

    def test_promotion_only_requires_baidu_package_for_full_baseline(self) -> None:
        promoter = load_tool("promote_release_channel")
        policy = json.loads(
            (ROOT / "release" / "release_policy.json").read_text(encoding="utf-8")
        )
        patch = {
            "filename": "LiveClipperPatch_1_to_2_v3.zip",
            "sources": [
                {
                    "name": "GitHub",
                    "url": "https://github.com/example/repo/releases/download/v2/patch.zip",
                }
            ],
        }
        promoter.validate_candidate_distribution(
            {"package": {"url": ""}, "patches": [patch]},
            "business_runtime",
            policy,
        )
        with self.assertRaisesRegex(ValueError, "must not include a full package"):
            promoter.validate_candidate_distribution(
                {
                    "package": {"url": "https://pan.baidu.com/s/example"},
                    "patches": [patch],
                },
                "business_runtime",
                policy,
            )
        with self.assertRaisesRegex(ValueError, "at least one signed patch"):
            promoter.validate_candidate_distribution(
                {"package": {"url": ""}, "patches": []},
                "business_runtime",
                policy,
            )
        promoter.validate_candidate_distribution(
            {
                "package": {"url": "https://pan.baidu.com/s/example"},
                "patches": [],
            },
            "full_baseline",
            policy,
        )
        with self.assertRaisesRegex(ValueError, "Baidu full-package URL"):
            promoter.validate_candidate_distribution(
                {"package": {"url": ""}, "patches": []},
                "full_baseline",
                policy,
            )

    def test_github_workflow_rejects_full_package_assets_and_tests_zip(self) -> None:
        workflow = (
            ROOT / ".github" / "workflows" / "publish-incremental-release.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("LiveClipperPatch_", workflow)
        self.assertIn("patch assets only", workflow)
        self.assertIn("archive.testzip()", workflow)

    def test_tracked_docs_do_not_require_oss_or_current_v2_release(self) -> None:
        entry = (ROOT / "docs" / "PACKAGING_WINDOW_ENTRY.md").read_text(
            encoding="utf-8"
        )
        process = (ROOT / "docs" / "RELEASE_PROCESS_V3.md").read_text(
            encoding="utf-8"
        )
        policy = (ROOT / "docs" / "RELEASE_POLICY.md").read_text(
            encoding="utf-8"
        )
        combined = "\n".join((entry, process, policy))
        self.assertIn("不再要求 OSS", entry)
        self.assertIn("百度网盘", combined)
        self.assertIn("GitHub Release", combined)
        self.assertNotIn("Every automatic patch has at least two", combined)


if __name__ == "__main__":
    unittest.main()
