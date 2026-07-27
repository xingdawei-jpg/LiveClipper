"""Read-only release preflight for LiveClipper Runtime V3."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import urllib.parse
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from release_signing import verify_manifest
from runtime_v3_versions import LAUNCHER_VERSION, UPDATER_VERSION
import build_update_manifest as manifest_builder

POLICY = ROOT / "release" / "release_policy.json"
BASELINES = ROOT / "release" / "baselines.json"
VERSION = ROOT / "app" / "version.json"
STABLE = ROOT / "release" / "stable.json"
PUBLIC_KEY = ROOT / "app" / "release_update_public_key.pem"
VERSION_RE = re.compile(r"^\d{4}\.\d{1,2}\.\d{1,2}\.\d+$")
SHA_RE = re.compile(r"^[0-9A-Fa-f]{64}$")


class Report:
    def __init__(self, phase: str) -> None:
        self.phase = phase
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.facts: dict[str, Any] = {}

    def error(self, value: str) -> None:
        self.errors.append(value)

    def warning(self, value: str) -> None:
        self.warnings.append(value)

    def result(self) -> dict[str, Any]:
        return {
            "ok": not self.errors,
            "phase": self.phase,
            "facts": self.facts,
            "warnings": self.warnings,
            "errors": self.errors,
        }


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root is not an object: {path}")
    return value


def version_key(value: str) -> tuple[int, ...]:
    if not VERSION_RE.fullmatch(str(value or "")):
        raise ValueError(f"invalid version: {value!r}")
    return tuple(int(part) for part in value.split("."))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def manifest_sha256(path: Path) -> str:
    data = path.read_bytes()
    if path.suffix.lower() in {
        ".py", ".json", ".txt", ".html", ".css", ".js", ".cs", ".spec", ".pem"
    }:
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def verify_signed(report: Report, value: dict[str, Any], label: str) -> None:
    try:
        verify_manifest(value, PUBLIC_KEY)
    except Exception as exc:
        report.error(f"{label} signature failed: {exc}")


def current_baseline(report: Report, registry: dict[str, Any]) -> dict[str, Any] | None:
    records = registry.get("baselines")
    current_version = str(registry.get("current_baseline_version") or "")
    if not isinstance(records, list):
        report.error("baseline registry records must be a list")
        return None
    current = [
        item for item in records
        if isinstance(item, dict) and item.get("status") == "current"
    ]
    if len(current) != 1 or str(current[0].get("version") or "") != current_version:
        report.error("baseline registry must identify exactly one current baseline")
        return None
    return current[0]


def inspect_full_zip(
    report: Report,
    path: Path,
    version: str,
    launcher: str,
    updater: str,
    *,
    full_test: bool,
    source_commit: str = "",
) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            current_names = [
                name for name in names
                if name.count("/") == 1 and name.endswith("/current.json")
            ]
            if len(current_names) != 1:
                raise ValueError("expected one package-root current.json")
            root = current_names[0].split("/", 1)[0]
            pointer = json.loads(archive.read(current_names[0]).decode("utf-8-sig"))
            actual_version = str(pointer.get("current_version") or "")
            install = json.loads(
                archive.read(f"{root}/install_manifest.json").decode("utf-8-sig")
            )
            runtime = json.loads(
                archive.read(
                    f"{root}/versions/{actual_version}/runtime_manifest.json"
                ).decode("utf-8-sig")
            )
            verify_signed(report, install, f"{path.name} install manifest")
            verify_signed(report, runtime, f"{path.name} runtime manifest")
            if actual_version != version or runtime.get("version") != version:
                report.error(f"{path.name}: runtime version mismatch")
            if install.get("launcher_version") != launcher:
                report.error(f"{path.name}: launcher version mismatch")
            if install.get("updater_version") != updater:
                report.error(f"{path.name}: updater version mismatch")
            if source_commit and runtime.get("source_commit") != source_commit:
                report.error(f"{path.name}: source commit mismatch")
            required = {
                f"{root}/LiveClipperWeb.exe",
                f"{root}/updater/LiveClipperUpdater.exe",
                f"{root}/updater/release_update_public_key.pem",
                (
                    f"{root}/versions/{actual_version}/_internal/"
                    "webview2_runtime/msedgewebview2.exe"
                ),
                (
                    f"{root}/versions/{actual_version}/_internal/"
                    "ffmpeg/ffmpeg.exe"
                ),
                (
                    f"{root}/versions/{actual_version}/_internal/"
                    "ffmpeg/ffprobe.exe"
                ),
            }
            if not required.issubset(names):
                report.error(
                    f"{path.name}: stable package files, fixed WebView2 runtime, "
                    "or bundled FFmpeg tools are missing"
                )
            if full_test:
                bad = archive.testzip()
                if bad:
                    report.error(f"{path.name}: corrupt ZIP entry {bad}")
    except Exception as exc:
        report.error(f"{path.name}: invalid full package: {exc}")


def validate_sources(
    report: Report,
    manifest: dict[str, Any],
    allowed_hosts: set[str],
    strict: bool,
) -> None:
    target = str(manifest.get("version") or manifest.get("latest_version") or "").strip()
    seen: set[tuple[str, str]] = set()
    graph: dict[str, set[str]] = {}
    patches = manifest.get("patches")
    if not isinstance(patches, list):
        report.error("channel patches must be a list")
        return
    for patch in patches:
        if not isinstance(patch, dict):
            report.error("patch record is not an object")
            continue
        edge = (
            str(patch.get("from_version") or ""),
            str(patch.get("to_version") or ""),
        )
        if not edge[0] or not edge[1] or edge[0] == edge[1]:
            report.error(f"invalid patch edge: {edge}")
        if edge in seen:
            report.error(f"duplicate patch edge: {edge}")
        seen.add(edge)
        graph.setdefault(edge[0], set()).add(edge[1])
        if int(patch.get("stable_payload_files") or 0) != 0:
            report.error(f"patch contains stable payload: {edge}")
        values: list[Any] = []
        if patch.get("url"):
            values.append(patch["url"])
        raw = patch.get("sources")
        if not isinstance(raw, list):
            raw = patch.get("urls")
        if isinstance(raw, list):
            values.extend(raw)
        urls: list[str] = []
        for value in values:
            url = str(value.get("url") or "") if isinstance(value, dict) else str(value or "")
            if url and url not in urls:
                urls.append(url)
        if len(urls) != 1:
            message = f"patch must have exactly one GitHub source: {edge}"
            (report.error if strict else report.warning)(message)
        for url in urls:
            parsed = urllib.parse.urlparse(url)
            safe = (
                parsed.scheme == "https"
                and parsed.hostname
                and not parsed.username
                and not parsed.password
            )
            if not safe:
                report.error(f"patch has unsafe source: {edge}")
            elif parsed.hostname not in allowed_hosts:
                message = f"patch uses legacy non-policy host {parsed.hostname}: {edge}"
                (report.error if strict else report.warning)(message)

    def reaches_target(start: str) -> bool:
        pending = [start]
        visited: set[str] = set()
        while pending:
            current = pending.pop()
            if current == target:
                return True
            if current in visited:
                continue
            visited.add(current)
            pending.extend(graph.get(current, ()))
        return False

    stranded = sorted(source for source in graph if not reaches_target(source))
    for source in stranded:
        report.error(f"patch graph has no route to channel target {target}: {source}")


def validate_artifact(
    report: Report,
    path: Path,
    record: dict[str, Any],
    label: str,
) -> bool:
    if not path.is_file():
        report.error(f"{label} is missing: {path}")
        return False
    if path.name != str(record.get("filename") or ""):
        report.error(f"{label} filename mismatch")
    if path.stat().st_size != int(record.get("size") or 0):
        report.error(f"{label} size mismatch")
        return False
    expected = str(record.get("sha256") or "").upper()
    if not SHA_RE.fullmatch(expected) or sha256_file(path) != expected:
        report.error(f"{label} SHA256 mismatch")
        return False
    return True


def validate_patch_zip(
    report: Report,
    path: Path,
    record: dict[str, Any],
) -> None:
    if not validate_artifact(report, path, record, f"patch {path.name}"):
        return
    try:
        with zipfile.ZipFile(path) as archive:
            bad = archive.testzip()
            if bad:
                raise ValueError(f"corrupt ZIP entry {bad}")
            patch = json.loads(
                archive.read("patch_manifest.json").decode("utf-8-sig")
            )
            verify_signed(report, patch, f"{path.name} patch manifest")
            for key in ("from_version", "to_version"):
                if patch.get(key) != record.get(key):
                    report.error(f"{path.name}: {key} differs from channel")
            if patch.get("stable_payload"):
                report.error(f"{path.name}: stable payload is forbidden")
    except Exception as exc:
        report.error(f"{path.name}: invalid patch: {exc}")


def _safe_scope_path(value: Any) -> str:
    text = str(value or "").replace("\\", "/").strip("/")
    path = PurePosixPath(text)
    if (
        not text
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or ":" in path.parts[0]
    ):
        raise ValueError(f"unsafe release scope path: {value!r}")
    return path.as_posix()


def load_release_scope(path: Path) -> dict[str, Any]:
    data = load_json(path)
    version = str(data.get("version") or "").strip()
    include = data.get("include")
    exclude = data.get("exclude")
    if not version or not isinstance(include, list) or not isinstance(exclude, list):
        raise ValueError("release scope requires version, include, and exclude lists")

    included: set[str] = set()
    excluded: set[str] = set()
    for item in include:
        if not isinstance(item, str):
            raise ValueError("release scope include entries must be paths")
        included.add(_safe_scope_path(item))
    for item in exclude:
        if not isinstance(item, dict):
            raise ValueError("release scope exclude entries must include a reason")
        reason = str(item.get("reason") or "").strip()
        if not reason:
            raise ValueError("release scope exclude entry is missing a reason")
        excluded.add(_safe_scope_path(item.get("path")))

    overlap = included & excluded
    if overlap:
        raise ValueError(f"release scope path is both included and excluded: {sorted(overlap)}")
    return {
        "version": version,
        "include": included,
        "exclude": excluded,
    }


def git_paths(report: Report, scope: dict[str, Any] | None = None) -> None:
    try:
        run = subprocess.run(
            ["git", "status", "--porcelain=v1", "-uall"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except Exception as exc:
        report.error(f"cannot read git status: {exc}")
        return
    paths = []
    for line in run.stdout.splitlines():
        value = line[3:].strip()
        paths.append(value.split(" -> ", 1)[-1].replace("\\", "/"))
    report.facts["worktree_changes"] = paths
    if not paths:
        return
    if report.phase == "development":
        report.warning(f"worktree has {len(paths)} changed files")
        return
    allowed_prefixes = (
        ("release/candidates/", "release/github/")
        if report.phase == "candidate"
        else ("release/stable.json", "release/candidates/")
    )
    declared: set[str] = set()
    if scope:
        included = set(scope.get("include") or ())
        excluded = set(scope.get("exclude") or ())
        declared = included | excluded
        report.facts["release_scope_include"] = sorted(included)
        report.facts["release_scope_exclude"] = sorted(excluded)
    bad = [
        path for path in paths
        if path not in declared
        and not any(path == item or path.startswith(item) for item in allowed_prefixes)
    ]
    if bad:
        report.error(f"{report.phase} has forbidden worktree changes: {bad}")


def run_preflight(
    phase: str,
    manifest_path: Path | None = None,
    package_path: Path | None = None,
    patch_paths: list[Path] | None = None,
    acceptance_path: Path | None = None,
    scope_path: Path | None = None,
) -> Report:
    report = Report(phase)
    try:
        policy = load_json(POLICY)
        registry = load_json(BASELINES)
        runtime = load_json(VERSION)
    except Exception as exc:
        report.error(f"cannot load release source of truth: {exc}")
        return report

    distribution = policy.get("distribution") or {}
    full_policy = distribution.get("full_package") or {}
    patch_policy = distribution.get("automatic_patch") or {}
    if full_policy.get("provider") != "baidu-netdisk":
        report.error("full package provider must be baidu-netdisk")
    if full_policy.get("publish_to_github_releases") is not False:
        report.error("GitHub full-package assets must be forbidden")
    if full_policy.get("share_url_hosts") != ["pan.baidu.com"]:
        report.error("full-package share host must be pan.baidu.com")
    if patch_policy.get("provider") != "github-releases":
        report.error("patch provider must be github-releases")
    hosts = set(patch_policy.get("manifest_url_hosts") or [])
    if hosts != {"github.com"} or int(patch_policy.get("minimum_sources") or 0) != 1:
        report.error("automatic patch policy must be one GitHub HTTPS source")

    versions = {
        str(runtime.get("version") or ""),
        str(runtime.get("latest_version") or ""),
        str(runtime.get("build_id") or ""),
    }
    runtime_version = str(runtime.get("version") or "")
    if len(versions) != 1 or "" in versions:
        report.error("runtime version, latest_version, and build_id must match")
    try:
        version_key(runtime_version)
    except ValueError as exc:
        report.error(str(exc))
    scope = None
    if scope_path is not None:
        try:
            scope = load_release_scope(scope_path)
            if scope["version"] != runtime_version:
                report.error("release scope version differs from runtime version")
        except Exception as exc:
            report.error(f"invalid release scope: {exc}")
    if runtime.get("schema_version") != 3 or runtime.get("runtime_layout_version") != 3:
        report.error("app/version.json must declare Runtime V3")
    if runtime.get("minimum_launcher_version") != LAUNCHER_VERSION:
        report.error("runtime launcher version differs from stable source")
    if runtime.get("minimum_updater_version") != UPDATER_VERSION:
        report.error("runtime updater version differs from stable source")
    if runtime.get("files") != {}:
        report.error("legacy files map must be empty")
    runtime_records = runtime.get("runtime_files") or {}
    expected_runtime_records = {
        path.relative_to(ROOT).as_posix()
        for path in manifest_builder.RUNTIME_FILES
        if path.exists()
    }
    if set(runtime_records) != expected_runtime_records:
        report.error("app/version.json runtime_files set is incomplete or stale")
    for relative, expected in runtime_records.items():
        try:
            path = (ROOT / relative).resolve()
            path.relative_to(ROOT.resolve())
        except Exception:
            report.error(f"unsafe runtime file path: {relative}")
            continue
        if not path.is_file() or manifest_sha256(path).lower() != str(expected).lower():
            report.error(f"stale runtime file hash: {relative}")

    source_records = runtime.get("source_files") or {}
    expected_source_records = {
        path.relative_to(ROOT).as_posix()
        for path in manifest_builder._source_files()
    }
    if set(source_records) != expected_source_records:
        message = "app/version.json source_files set is incomplete or stale"
        (report.warning if phase == "development" else report.error)(message)
    for relative, expected in source_records.items():
        try:
            path = (ROOT / relative).resolve()
            path.relative_to(ROOT.resolve())
        except Exception:
            report.error(f"unsafe source file path: {relative}")
            continue
        if not path.is_file() or manifest_sha256(path).lower() != str(expected).lower():
            message = f"stale source provenance hash: {relative}"
            (report.warning if phase == "development" else report.error)(message)

    baseline = current_baseline(report, registry)
    if baseline:
        baseline_version = str(baseline.get("version") or "")
        report.facts["current_baseline_version"] = baseline_version
        try:
            if version_key(baseline_version) > version_key(runtime_version):
                report.error("baseline is newer than runtime source")
        except ValueError as exc:
            report.error(str(exc))
        if baseline.get("launcher_version") != LAUNCHER_VERSION:
            report.error("baseline launcher differs from stable source")
        if baseline.get("updater_version") != UPDATER_VERSION:
            report.error("baseline updater differs from stable source")
        package = baseline.get("package") or {}
        local = ROOT / "release_dist" / str(package.get("filename") or "")
        report.facts["current_baseline_package"] = local.name
        if local.is_file() and validate_artifact(report, local, package, "baseline ZIP"):
            inspect_full_zip(
                report,
                local,
                baseline_version,
                str(baseline.get("launcher_version") or ""),
                str(baseline.get("updater_version") or ""),
                full_test=False,
                source_commit=str(baseline.get("runtime_source_commit") or ""),
            )
        elif not local.is_file():
            report.warning("current baseline ZIP is absent from release_dist")

    channel_path = manifest_path or STABLE
    try:
        channel = load_json(channel_path)
    except Exception as exc:
        report.error(f"cannot load channel manifest: {exc}")
        git_paths(report)
        return report
    verify_signed(report, channel, str(channel_path))
    channel_version = str(channel.get("version") or channel.get("latest_version") or "")
    status = str(channel.get("channel_status") or "")
    report.facts.update(
        runtime_version=runtime_version,
        launcher_version=LAUNCHER_VERSION,
        updater_version=UPDATER_VERSION,
        channel_version=channel_version,
        channel_status=status,
    )
    expected_status = {"candidate": "hold", "publish": "ready"}.get(phase)
    if expected_status and status != expected_status:
        report.error(f"{phase} channel must be {expected_status}")
    if phase == "candidate":
        try:
            channel_path.resolve().relative_to(
                (ROOT / "release" / "candidates").resolve()
            )
        except ValueError:
            report.error("candidate manifest must be under release/candidates")
    if phase == "publish" and channel_path.resolve() != STABLE.resolve():
        report.error("publish phase must validate release/stable.json")
    try:
        channel_key = version_key(channel_version)
        runtime_key = version_key(runtime_version)
        if channel_key > runtime_key:
            report.error("channel cannot be newer than runtime source")
        if phase != "development" and channel_key != runtime_key:
            report.error("candidate/publish channel must equal runtime source")
        effective = version_key(str(policy.get("effective_after_baseline") or ""))
        strict = phase != "development" or channel_key > effective
    except ValueError as exc:
        report.error(str(exc))
        strict = phase != "development"
    if phase == "development" and channel_version != runtime_version:
        report.warning(
            f"runtime {runtime_version} is ahead of live channel {channel_version}"
        )
    if phase != "development":
        if channel.get("minimum_launcher_version") != runtime.get("minimum_launcher_version"):
            report.error("channel launcher requirement differs from runtime")
        if channel.get("minimum_updater_version") != runtime.get("minimum_updater_version"):
            report.error("channel updater requirement differs from runtime")
    validate_sources(report, channel, hosts, strict)

    stable_changed = bool(
        baseline
        and (
            channel.get("minimum_launcher_version") != baseline.get("launcher_version")
            or channel.get("minimum_updater_version") != baseline.get("updater_version")
        )
    )
    declared_release_type = str(channel.get("release_type") or "").strip()
    inferred_release_type = "full_baseline" if stable_changed else "business_runtime"
    release_type = declared_release_type or inferred_release_type
    report.facts["release_type"] = release_type
    if release_type not in {"business_runtime", "full_baseline"}:
        report.error("invalid channel release_type")
    elif phase != "development" and not declared_release_type:
        report.error("candidate/publish channel must declare release_type")
    if release_type == "business_runtime" and stable_changed:
        report.error("business_runtime cannot change stable components")
    github_spec = ROOT / "release" / "github" / f"v{channel_version}.json"
    require_patch_spec = (
        phase != "development"
        and release_type == "business_runtime"
    )
    if github_spec.is_file():
        spec = load_json(github_spec)
        invalid = [
            str(item.get("name") or "")
            for item in (spec.get("assets") or [])
            if not (
                isinstance(item, dict)
                and str(item.get("name") or "").startswith("LiveClipperPatch_")
                and (
                    str(item.get("name") or "").endswith(".zip")
                    or str(item.get("name") or "").endswith(".zip.sha256.txt")
                )
            )
        ]
        if invalid:
            message = f"GitHub spec contains non-patch assets: {invalid}"
            (report.error if require_patch_spec else report.warning)(message)
    elif require_patch_spec:
        report.error(f"missing GitHub patch spec: {github_spec}")

    patches = {
        str(item.get("filename") or ""): item
        for item in (channel.get("patches") or [])
        if isinstance(item, dict)
    }
    if phase != "development":
        package_record = channel.get("package")
        package_url = (
            str(package_record.get("url") or "")
            if isinstance(package_record, dict)
            else ""
        )
        package_has_payload = bool(
            isinstance(package_record, dict)
            and any(
                (
                    package_url,
                    str(package_record.get("sha256") or ""),
                    str(package_record.get("filename") or ""),
                    package_record.get("size"),
                )
            )
        )
        if release_type == "full_baseline":
            if not isinstance(package_record, dict) or package_path is None:
                report.error("full_baseline candidate/publish requires --package")
            elif validate_artifact(report, package_path, package_record, "full package"):
                inspect_full_zip(
                    report,
                    package_path,
                    channel_version,
                    str(channel.get("minimum_launcher_version") or ""),
                    str(channel.get("minimum_updater_version") or ""),
                    full_test=True,
                )
        elif not isinstance(package_record, dict):
            report.error("business_runtime package metadata is invalid")
        elif package_path is not None or package_has_payload:
            report.error("business_runtime must not include a full package")
        provided = {path.name: path for path in (patch_paths or [])}
        if set(provided) != set(patches):
            report.error("--patch files must exactly match channel patches")
        for name, record in patches.items():
            if name in provided:
                validate_patch_zip(report, provided[name], record)
        if release_type == "full_baseline" and patches:
            report.error("full_baseline cannot contain ordinary runtime patches")
        if release_type == "business_runtime" and not patches:
            report.error("business_runtime release requires at least one signed patch")

    if phase == "publish":
        if acceptance_path is None:
            report.error("publish requires --acceptance")
        else:
            try:
                acceptance = load_json(acceptance_path)
                release_type = str(acceptance.get("release_type") or "")
                if acceptance.get("version") != channel_version:
                    report.error("acceptance version differs from channel")
                if release_type not in {"business_runtime", "full_baseline"}:
                    report.error("invalid acceptance release_type")
                if release_type != str(channel.get("release_type") or ""):
                    report.error("acceptance release_type differs from channel")
                required = [
                    *(policy.get("acceptance_gates", {}).get("always") or []),
                    *(policy.get("acceptance_gates", {}).get(release_type) or []),
                ]
                gates = acceptance.get("gates") or {}
                evidence = acceptance.get("evidence") or {}
                for gate in required:
                    if gates.get(gate) != "pass":
                        report.error(f"acceptance gate is not pass: {gate}")
                    if not evidence.get(gate):
                        report.error(f"acceptance evidence is missing: {gate}")
            except Exception as exc:
                report.error(f"invalid acceptance evidence: {exc}")

    git_paths(report, scope)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase",
        choices=("development", "candidate", "publish"),
        default="development",
    )
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--package", type=Path)
    parser.add_argument("--patch", type=Path, action="append", default=[])
    parser.add_argument("--acceptance", type=Path)
    parser.add_argument("--scope", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = run_preflight(
        args.phase,
        args.manifest.resolve() if args.manifest else None,
        args.package.resolve() if args.package else None,
        [path.resolve() for path in args.patch],
        args.acceptance.resolve() if args.acceptance else None,
        args.scope.resolve() if args.scope else None,
    )
    result = report.result()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            f"[{'PASS' if result['ok'] else 'FAIL'}] "
            f"LiveClipper release preflight: {args.phase}"
        )
        for key, value in result["facts"].items():
            print(f"  {key}: {value}")
        for value in result["warnings"]:
            print(f"  WARNING: {value}")
        for value in result["errors"]:
            print(f"  ERROR: {value}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
