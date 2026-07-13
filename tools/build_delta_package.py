"""Build a signed Runtime V2/V3 to Runtime V3 LiveClipper delta archive."""

from __future__ import annotations

import argparse
import sys
import hashlib
import json
import re
import shutil
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

APP_IMPORT_DIR = Path(__file__).resolve().parents[1] / "app"
if APP_IMPORT_DIR.is_dir() and str(APP_IMPORT_DIR) not in sys.path:
    sys.path.insert(0, str(APP_IMPORT_DIR))


from release_signing import sha256_file, sign_manifest, verify_manifest


PATCH_FORMAT = "liveclipper-version-delta-v1"
RUNTIME_MANIFEST_FORMAT = "liveclipper-runtime-manifest-v1"
RUNTIME_MANIFEST = "runtime_manifest.json"
INSTALL_MANIFEST = "install_manifest.json"
DEFAULT_ENTRYPOINT = "LiveClipperWeb.exe"
VERSION_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,79}$")


class PackageError(RuntimeError):
    pass


def _safe_version(value: Any) -> str:
    version = str(value or "").strip()
    if not VERSION_PATTERN.fullmatch(version) or version.startswith("."):
        raise PackageError(f"invalid version: {value!r}")
    return version


def _safe_relative(value: str) -> str:
    text = str(value or "").replace("\\", "/").strip("/")
    path = PurePosixPath(text)
    if not text or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PackageError(f"unsafe package path: {value!r}")
    if ":" in path.parts[0]:
        raise PackageError(f"unsafe package path: {value!r}")
    return path.as_posix()


class PackageView:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.archive = zipfile.ZipFile(self.path)
        file_names = [
            _safe_relative(info.filename)
            for info in self.archive.infolist()
            if not info.is_dir()
        ]
        roots = {PurePosixPath(name).parts[0] for name in file_names}
        if len(roots) != 1:
            raise PackageError(f"package must contain one root directory: {self.path}")
        self.root = next(iter(roots))
        self._infos = {name: self.archive.getinfo(name) for name in file_names}
        self._hashes: dict[str, str] = {}

    def close(self) -> None:
        self.archive.close()

    def _name(self, relative: str) -> str:
        relative = _safe_relative(relative)
        return (PurePosixPath(self.root) / relative).as_posix()

    def has(self, relative: str) -> bool:
        return self._name(relative) in self._infos

    def info(self, relative: str) -> zipfile.ZipInfo:
        name = self._name(relative)
        try:
            return self._infos[name]
        except KeyError as exc:
            raise PackageError(f"package file is missing: {relative}") from exc

    def read(self, relative: str) -> bytes:
        return self.archive.read(self._name(relative))

    def json(self, relative: str) -> dict[str, Any]:
        data = json.loads(self.read(relative).decode("utf-8-sig"))
        if not isinstance(data, dict):
            raise PackageError(f"{relative} is not an object")
        return data

    def sha256(self, relative: str) -> str:
        name = self._name(relative)
        cached = self._hashes.get(name)
        if cached:
            return cached
        digest = hashlib.sha256()
        with self.archive.open(name) as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        result = digest.hexdigest().lower()
        self._hashes[name] = result
        return result

    def files_under(self, prefix: str = "") -> list[str]:
        clean = _safe_relative(prefix) if prefix else ""
        archive_prefix = (PurePosixPath(self.root) / clean).as_posix().rstrip("/")
        if clean:
            archive_prefix += "/"
        else:
            archive_prefix = self.root + "/"
        result = []
        for name in self._infos:
            if name.startswith(archive_prefix):
                result.append(name[len(archive_prefix) :])
        return sorted(result)

    def stream_to(self, relative: str, output: zipfile.ZipFile, archive_name: str) -> None:
        with self.archive.open(self._name(relative)) as source, output.open(archive_name, "w") as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)


def _runtime_descriptor(
    package: PackageView,
    private_key: Path,
    public_key: Path,
) -> tuple[int, str, str, dict[str, Any]]:
    if package.has("current.json"):
        state = package.json("current.json")
        if int(state.get("runtime_layout_version") or 0) != 3:
            raise PackageError("current.json is not Runtime V3")
        version = _safe_version(state.get("current_version"))
        prefix = f"versions/{version}"
        manifest = package.json(f"{prefix}/{RUNTIME_MANIFEST}")
        verify_manifest(manifest, public_key)
        if manifest.get("format") != RUNTIME_MANIFEST_FORMAT:
            raise PackageError("unsupported runtime manifest format")
        if _safe_version(manifest.get("version")) != version:
            raise PackageError("runtime manifest version mismatch")
        return 3, version, prefix, manifest

    version_data = package.json("_internal/app/version.json")
    version = _safe_version(version_data.get("version") or version_data.get("latest_version"))
    files: dict[str, dict[str, object]] = {}
    for relative in package.files_under():
        info = package.info(relative)
        files[relative] = {
            "sha256": package.sha256(relative),
            "size": info.file_size,
        }
    manifest = sign_manifest(
        {
            "schema_version": 3,
            "format": RUNTIME_MANIFEST_FORMAT,
            "runtime_layout_version": 2,
            "version": version,
            "entrypoint": DEFAULT_ENTRYPOINT,
            "source_package_sha256": sha256_file(package.path),
            "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "files": files,
        },
        private_key,
    )
    return 2, version, "", manifest


def _runtime_file_relative(prefix: str, relative: str) -> str:
    return (PurePosixPath(prefix) / relative).as_posix() if prefix else relative


def _verify_runtime_package(
    package: PackageView,
    prefix: str,
    manifest: dict[str, Any],
) -> None:
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise PackageError("runtime manifest has no files")
    for relative, meta in files.items():
        relative = _safe_relative(relative)
        package_relative = _runtime_file_relative(prefix, relative)
        info = package.info(package_relative)
        expected_size = (meta or {}).get("size")
        if expected_size is None or info.file_size != int(expected_size):
            raise PackageError(f"runtime size mismatch: {relative}")
        if package.sha256(package_relative) != str((meta or {}).get("sha256") or "").lower():
            raise PackageError(f"runtime hash mismatch: {relative}")


def _same_file(
    source: PackageView,
    source_relative: str,
    target_meta: dict[str, Any],
) -> bool:
    if not source.has(source_relative):
        return False
    info = source.info(source_relative)
    expected_size = target_meta.get("size")
    if expected_size is None or info.file_size != int(expected_size):
        return False
    return source.sha256(source_relative) == str(target_meta.get("sha256") or "").lower()


def build_patch(
    source_zip: Path,
    target_zip: Path,
    output_patch: Path,
    private_key: Path,
    public_key: Path,
) -> dict[str, Any]:
    source = PackageView(source_zip)
    target = PackageView(target_zip)
    try:
        source_layout, from_version, source_prefix, source_manifest = _runtime_descriptor(
            source,
            private_key,
            public_key,
        )
        target_layout, to_version, target_prefix, target_manifest = _runtime_descriptor(
            target,
            private_key,
            public_key,
        )
        if target_layout != 3:
            raise PackageError("target package must use Runtime V3")
        if from_version == to_version:
            raise PackageError("source and target versions must differ")
        _verify_runtime_package(source, source_prefix, source_manifest)
        _verify_runtime_package(target, target_prefix, target_manifest)

        install_manifest = target.json(INSTALL_MANIFEST)
        verify_manifest(install_manifest, public_key)
        stable_result = install_manifest.get("files")
        if not isinstance(stable_result, dict) or not stable_result:
            raise PackageError("target install manifest has no stable files")

        runtime_payload: dict[str, dict[str, Any]] = {}
        target_files = target_manifest.get("files") or {}
        for relative, meta in target_files.items():
            relative = _safe_relative(relative)
            source_relative = _runtime_file_relative(source_prefix, relative)
            if not _same_file(source, source_relative, meta):
                runtime_payload[relative] = {
                    "archive": f"payload/runtime/{relative}",
                    "sha256": str(meta.get("sha256") or "").lower(),
                    "size": int(meta.get("size") or 0),
                }

        stable_payload: dict[str, dict[str, Any]] = {}
        for relative, meta in stable_result.items():
            relative = _safe_relative(relative)
            if source_layout == 2 or not _same_file(source, relative, meta):
                stable_payload[relative] = {
                    "archive": f"payload/stable/{relative}",
                    "sha256": str(meta.get("sha256") or "").lower(),
                    "size": int(meta.get("size") or 0),
                }

        if source_layout == 3 and stable_payload:
            changed = ", ".join(sorted(stable_payload))
            raise PackageError(
                "Runtime V3 delta cannot replace stable components in place; "
                "reuse the exact launcher, updater, and trust key from the source package "
                f"or publish a signed bridge/full package. Changed: {changed}"
            )

        patch_manifest = sign_manifest(
            {
                "schema_version": 3,
                "format": PATCH_FORMAT,
                "from_version": from_version,
                "to_version": to_version,
                "source_layout_version": source_layout,
                "target_layout_version": 3,
                "source_package_sha256": sha256_file(source.path),
                "target_package_sha256": sha256_file(target.path),
                "source_runtime_manifest": source_manifest,
                "target_runtime_manifest": target_manifest,
                "target_install_manifest": install_manifest,
                "runtime_payload": runtime_payload,
                "stable_payload": stable_payload,
                "stable_result_files": stable_result,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            },
            private_key,
        )

        output_patch = output_patch.resolve()
        output_patch.parent.mkdir(parents=True, exist_ok=True)
        if output_patch.exists():
            raise FileExistsError(output_patch)
        with zipfile.ZipFile(
            output_patch,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            allowZip64=True,
        ) as archive:
            for relative, meta in runtime_payload.items():
                target_relative = _runtime_file_relative(target_prefix, relative)
                target.stream_to(target_relative, archive, meta["archive"])
            for relative, meta in stable_payload.items():
                target.stream_to(relative, archive, meta["archive"])
            archive.writestr(
                "patch_manifest.json",
                json.dumps(patch_manifest, ensure_ascii=False, indent=2) + "\n",
            )
        patch_hash = sha256_file(output_patch).upper()
        output_patch.with_suffix(output_patch.suffix + ".sha256.txt").write_text(
            f"{patch_hash}  {output_patch.name}\n",
            encoding="ascii",
        )
        return {
            "format": PATCH_FORMAT,
            "from_version": from_version,
            "to_version": to_version,
            "source_layout_version": source_layout,
            "target_layout_version": 3,
            "filename": output_patch.name,
            "sha256": patch_hash,
            "size": output_patch.stat().st_size,
            "runtime_payload_files": len(runtime_payload),
            "stable_payload_files": len(stable_payload),
        }
    finally:
        source.close()
        target.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a signed LiveClipper Runtime V3 patch.")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--public-key", type=Path, required=True)
    args = parser.parse_args()
    summary = build_patch(
        args.source,
        args.target,
        args.output,
        args.private_key,
        args.public_key,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
