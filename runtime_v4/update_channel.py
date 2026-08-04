"""Signed update-channel and verified download support for Runtime V4."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
import ssl
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from runtime_v4.business_bundle import verify_business_archive


CHANNEL_SCHEMA_VERSION = 1
CHANNEL_FORMAT = "liveclipper-runtime-v4-business-channel-v1"
RUNTIME_LAYOUT_VERSION = 4
SIGNATURE_ALGORITHM = "ed25519"
ACTIVE_CHANNEL_STATUS = "ready"
INACTIVE_CHANNEL_STATUSES = frozenset({"hold", "paused", "disabled"})
ALLOWED_CHANNEL_STATUSES = frozenset({ACTIVE_CHANNEL_STATUS, *INACTIVE_CHANNEL_STATUSES})
VERSION_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]+){1,3}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
FILENAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+\.zip$")
MAX_CHANNEL_BYTES = 1024 * 1024
MAX_BUNDLE_DOWNLOAD_SIZE = 256 * 1024 * 1024
DOWNLOAD_CHUNK_SIZE = 1024 * 1024
DOWNLOAD_ATTEMPTS_PER_SOURCE = 2
DOWNLOAD_SOCKET_TIMEOUT = 30
DOWNLOAD_TOTAL_TIMEOUT = 300


class UpdateChannelError(RuntimeError):
    pass


@dataclass(frozen=True)
class UpdateSource:
    name: str
    url: str


@dataclass(frozen=True)
class VerifiedUpdateChannel:
    channel: str
    channel_status: str
    application_version: str
    allowed_source_versions: tuple[str, ...]
    compatible_core_versions: tuple[str, ...]
    filename: str
    size: int
    sha256: str
    bundle_manifest_sha256: str
    sources: tuple[UpdateSource, ...]
    release_notes: str
    published_at: str
    signature_key_id: str
    document_sha256: str


@dataclass(frozen=True)
class BusinessUpdateDecision:
    available: bool
    reason: str
    current_version: str
    current_core_version: str
    channel: VerifiedUpdateChannel


@dataclass(frozen=True)
class BusinessUpdateApplyResult:
    decision: BusinessUpdateDecision
    archive_path: Path | None
    install_result: Any | None


DownloadProgress = Callable[[int, int, str], None]


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(DOWNLOAD_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def _public_key_id(key: Ed25519PublicKey) -> str:
    raw = key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return hashlib.sha256(raw).hexdigest()[:16]


def _load_private_key(path: Path) -> Ed25519PrivateKey:
    try:
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except Exception as exc:
        raise UpdateChannelError(f"cannot load Runtime V4 channel private key: {path}") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise UpdateChannelError("Runtime V4 channel private key is not Ed25519")
    return key


def _load_public_key(path: Path) -> Ed25519PublicKey:
    try:
        key = serialization.load_pem_public_key(path.read_bytes())
    except Exception as exc:
        raise UpdateChannelError(f"cannot load Runtime V4 channel public key: {path}") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise UpdateChannelError("Runtime V4 channel public key is not Ed25519")
    return key


def _safe_version(value: object, *, label: str) -> str:
    version = str(value or "").strip()
    if not VERSION_PATTERN.fullmatch(version):
        raise UpdateChannelError(f"invalid {label}: {version!r}")
    return version


def _version_key(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def _require_exact_keys(
    value: Mapping[str, Any],
    expected: set[str],
    *,
    label: str,
) -> None:
    actual = set(value)
    if actual != expected:
        raise UpdateChannelError(
            f"Runtime V4 {label} fields mismatch: "
            f"extra={sorted(actual - expected)}, missing={sorted(expected - actual)}"
        )


def _validate_versions(values: object, *, label: str) -> tuple[str, ...]:
    if not isinstance(values, list) or not values:
        raise UpdateChannelError(f"Runtime V4 {label} must be a non-empty list")
    result = tuple(_safe_version(value, label=label) for value in values)
    if len(set(result)) != len(result):
        raise UpdateChannelError(f"Runtime V4 {label} contains duplicates")
    return result


def _validate_https_url(value: object, *, label: str) -> str:
    url = str(value or "").strip()
    parsed = urllib.parse.urlparse(url)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise UpdateChannelError(f"Runtime V4 {label} must be a credential-free HTTPS URL")
    return url


def _normalize_sources(values: object) -> tuple[UpdateSource, ...]:
    if not isinstance(values, list) or not values:
        raise UpdateChannelError("Runtime V4 business update has no download sources")
    sources: list[UpdateSource] = []
    seen: set[str] = set()
    for index, raw in enumerate(values, start=1):
        if not isinstance(raw, dict):
            raise UpdateChannelError("Runtime V4 update source must be an object")
        _require_exact_keys(raw, {"name", "url"}, label="update source")
        name = str(raw.get("name") or "").strip()
        if not name or len(name) > 80:
            raise UpdateChannelError(f"invalid Runtime V4 update source name at index {index}")
        url = _validate_https_url(raw.get("url"), label="update source")
        if url in seen:
            raise UpdateChannelError("Runtime V4 update channel contains duplicate sources")
        seen.add(url)
        sources.append(UpdateSource(name=name, url=url))
    return tuple(sources)


def _unsigned_document(document: Mapping[str, Any]) -> dict[str, Any]:
    unsigned = copy.deepcopy(dict(document))
    unsigned.pop("signature", None)
    return unsigned


def _validated_unsigned(document: Mapping[str, Any]) -> dict[str, Any]:
    expected_fields = {
        "schema_version",
        "format",
        "channel",
        "channel_status",
        "runtime_layout_version",
        "target",
        "business_bundle",
        "release_notes",
        "published_at",
    }
    _require_exact_keys(document, expected_fields, label="update channel")
    if document.get("schema_version") != CHANNEL_SCHEMA_VERSION:
        raise UpdateChannelError("unsupported Runtime V4 update channel schema")
    if document.get("format") != CHANNEL_FORMAT:
        raise UpdateChannelError("unsupported Runtime V4 update channel format")
    if document.get("runtime_layout_version") != RUNTIME_LAYOUT_VERSION:
        raise UpdateChannelError("Runtime V4 update channel layout mismatch")
    channel = str(document.get("channel") or "").strip()
    if not channel or len(channel) > 40 or not re.fullmatch(r"[a-z0-9_-]+", channel):
        raise UpdateChannelError("invalid Runtime V4 channel name")
    channel_status = str(document.get("channel_status") or "").strip().lower()
    if channel_status not in ALLOWED_CHANNEL_STATUSES:
        raise UpdateChannelError("invalid Runtime V4 channel status")

    target = document.get("target")
    if not isinstance(target, dict):
        raise UpdateChannelError("Runtime V4 update target must be an object")
    _require_exact_keys(
        target,
        {"application_version", "allowed_source_versions", "compatible_core_versions"},
        label="update target",
    )
    application_version = _safe_version(
        target.get("application_version"),
        label="target application version",
    )
    allowed_sources = _validate_versions(
        target.get("allowed_source_versions"),
        label="allowed source versions",
    )
    compatible_cores = _validate_versions(
        target.get("compatible_core_versions"),
        label="compatible core versions",
    )
    if any(_version_key(source) >= _version_key(application_version) for source in allowed_sources):
        raise UpdateChannelError("Runtime V4 source versions must be older than the target")

    artifact = document.get("business_bundle")
    if not isinstance(artifact, dict):
        raise UpdateChannelError("Runtime V4 business bundle metadata must be an object")
    _require_exact_keys(
        artifact,
        {"filename", "size", "sha256", "manifest_sha256", "sources"},
        label="business bundle",
    )
    filename = str(artifact.get("filename") or "").strip()
    if not FILENAME_PATTERN.fullmatch(filename):
        raise UpdateChannelError("invalid Runtime V4 business bundle filename")
    try:
        size = int(artifact.get("size"))
    except (TypeError, ValueError) as exc:
        raise UpdateChannelError("invalid Runtime V4 business bundle size") from exc
    if size <= 0 or size > MAX_BUNDLE_DOWNLOAD_SIZE:
        raise UpdateChannelError("Runtime V4 business bundle size exceeds its limit")
    archive_sha256 = str(artifact.get("sha256") or "").strip().lower()
    manifest_sha256 = str(artifact.get("manifest_sha256") or "").strip().lower()
    if not SHA256_PATTERN.fullmatch(archive_sha256):
        raise UpdateChannelError("invalid Runtime V4 business bundle SHA256")
    if not SHA256_PATTERN.fullmatch(manifest_sha256):
        raise UpdateChannelError("invalid Runtime V4 business manifest SHA256")
    sources = _normalize_sources(artifact.get("sources"))

    release_notes = str(document.get("release_notes") or "")
    published_at = str(document.get("published_at") or "")
    if len(release_notes) > 20_000 or len(published_at) > 80:
        raise UpdateChannelError("Runtime V4 update channel text fields exceed their limits")
    return {
        "channel": channel,
        "channel_status": channel_status,
        "application_version": application_version,
        "allowed_source_versions": allowed_sources,
        "compatible_core_versions": compatible_cores,
        "filename": filename,
        "size": size,
        "sha256": archive_sha256,
        "manifest_sha256": manifest_sha256,
        "sources": sources,
        "release_notes": release_notes,
        "published_at": published_at,
    }


def verify_update_channel(
    document: Mapping[str, Any],
    public_key_path: str | Path,
) -> VerifiedUpdateChannel:
    if not isinstance(document, Mapping):
        raise UpdateChannelError("Runtime V4 update channel must be a JSON object")
    _require_exact_keys(
        document,
        {
            "schema_version",
            "format",
            "channel",
            "channel_status",
            "runtime_layout_version",
            "target",
            "business_bundle",
            "release_notes",
            "published_at",
            "signature",
        },
        label="signed update channel",
    )
    signature = document.get("signature")
    if not isinstance(signature, dict):
        raise UpdateChannelError("Runtime V4 update channel has no signature")
    _require_exact_keys(signature, {"algorithm", "key_id", "value"}, label="channel signature")
    if signature.get("algorithm") != SIGNATURE_ALGORITHM:
        raise UpdateChannelError("unsupported Runtime V4 channel signature algorithm")
    public_key = _load_public_key(Path(public_key_path).resolve())
    key_id = _public_key_id(public_key)
    if str(signature.get("key_id") or "") != key_id:
        raise UpdateChannelError("Runtime V4 channel signature key id mismatch")
    unsigned = _unsigned_document(document)
    try:
        value = base64.b64decode(str(signature.get("value") or ""), validate=True)
        public_key.verify(value, _canonical_json_bytes(unsigned))
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise UpdateChannelError("Runtime V4 channel signature verification failed") from exc
    fields = _validated_unsigned(unsigned)
    return VerifiedUpdateChannel(
        channel=fields["channel"],
        channel_status=fields["channel_status"],
        application_version=fields["application_version"],
        allowed_source_versions=fields["allowed_source_versions"],
        compatible_core_versions=fields["compatible_core_versions"],
        filename=fields["filename"],
        size=fields["size"],
        sha256=fields["sha256"],
        bundle_manifest_sha256=fields["manifest_sha256"],
        sources=fields["sources"],
        release_notes=fields["release_notes"],
        published_at=fields["published_at"],
        signature_key_id=key_id,
        document_sha256=hashlib.sha256(_canonical_json_bytes(dict(document))).hexdigest(),
    )


def build_signed_update_channel(
    output_path: str | Path,
    archive_path: str | Path,
    *,
    application_version: str,
    allowed_source_versions: Sequence[str],
    compatible_core_versions: Sequence[str],
    sources: Sequence[Mapping[str, str]],
    private_key_path: str | Path,
    channel: str = "stable",
    channel_status: str = ACTIVE_CHANNEL_STATUS,
    release_notes: str = "",
    published_at: str = "",
) -> VerifiedUpdateChannel:
    output = Path(output_path).resolve()
    archive = Path(archive_path).resolve()
    if not archive.is_file():
        raise UpdateChannelError(f"Runtime V4 business archive does not exist: {archive}")
    private_key = _load_private_key(Path(private_key_path).resolve())
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    target_version = _safe_version(application_version, label="target application version")
    requested_cores = tuple(
        _safe_version(value, label="compatible core version")
        for value in compatible_core_versions
    )
    with tempfile.TemporaryDirectory(prefix="liveclipper-v4-channel-") as temporary:
        public_key_path = Path(temporary) / "public.pem"
        public_key_path.write_bytes(public_bytes)
        try:
            bundle = verify_business_archive(
                archive,
                public_key_path,
                expected_version=target_version,
            )
        except Exception as exc:
            raise UpdateChannelError("Runtime V4 channel archive failed bundle verification") from exc
        if tuple(bundle.compatible_core_versions) != requested_cores:
            raise UpdateChannelError(
                "Runtime V4 channel core compatibility differs from the signed business bundle"
            )

        unsigned = {
            "schema_version": CHANNEL_SCHEMA_VERSION,
            "format": CHANNEL_FORMAT,
            "channel": str(channel),
            "channel_status": str(channel_status),
            "runtime_layout_version": RUNTIME_LAYOUT_VERSION,
            "target": {
                "application_version": target_version,
                "allowed_source_versions": [str(value) for value in allowed_source_versions],
                "compatible_core_versions": list(requested_cores),
            },
            "business_bundle": {
                "filename": archive.name,
                "size": archive.stat().st_size,
                "sha256": _sha256_file(archive),
                "manifest_sha256": bundle.manifest_sha256,
                "sources": [dict(value) for value in sources],
            },
            "release_notes": str(release_notes or ""),
            "published_at": str(published_at or ""),
        }
        _validated_unsigned(unsigned)
        signature = private_key.sign(_canonical_json_bytes(unsigned))
        document = dict(unsigned)
        document["signature"] = {
            "algorithm": SIGNATURE_ALGORITHM,
            "key_id": _public_key_id(private_key.public_key()),
            "value": base64.b64encode(signature).decode("ascii"),
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary_output = output.with_name(f".{output.name}.tmp")
        try:
            temporary_output.write_bytes(_canonical_json_bytes(document))
            os.replace(temporary_output, output)
        finally:
            temporary_output.unlink(missing_ok=True)
        return verify_update_channel(document, public_key_path)


def load_update_channel_bytes(
    payload: bytes,
    public_key_path: str | Path,
) -> VerifiedUpdateChannel:
    if len(payload) > MAX_CHANNEL_BYTES:
        raise UpdateChannelError("Runtime V4 update channel exceeds its size limit")
    try:
        document = json.loads(payload.decode("utf-8"))
    except Exception as exc:
        raise UpdateChannelError("cannot decode Runtime V4 update channel JSON") from exc
    if not isinstance(document, dict):
        raise UpdateChannelError("Runtime V4 update channel must be a JSON object")
    return verify_update_channel(document, public_key_path)


def plan_business_update(
    channel: VerifiedUpdateChannel,
    *,
    current_version: str,
    current_core_version: str,
) -> BusinessUpdateDecision:
    current = _safe_version(current_version, label="current application version")
    core = _safe_version(current_core_version, label="current core version")
    if channel.channel_status != ACTIVE_CHANNEL_STATUS:
        return BusinessUpdateDecision(False, f"channel_{channel.channel_status}", current, core, channel)
    if _version_key(channel.application_version) <= _version_key(current):
        return BusinessUpdateDecision(False, "up_to_date", current, core, channel)
    if current not in channel.allowed_source_versions:
        return BusinessUpdateDecision(False, "source_version_not_allowed", current, core, channel)
    if core not in channel.compatible_core_versions:
        return BusinessUpdateDecision(False, "core_incompatible", current, core, channel)
    return BusinessUpdateDecision(True, "update_available", current, core, channel)


def _emit_progress(
    callback: DownloadProgress | None,
    downloaded: int,
    total: int,
    message: str,
) -> None:
    if callback is not None:
        callback(downloaded, total, message)


def _safe_error(exc: Exception) -> str:
    text = str(exc or type(exc).__name__).strip()
    text = re.sub(r"https?://[^\s;,]+", "download source", text, flags=re.I)
    return (text or type(exc).__name__)[:240]


def _open_url(opener: Callable[..., Any], request: urllib.request.Request, timeout: int) -> Any:
    return opener(request, timeout=timeout, context=ssl.create_default_context())


def _download_from_source(
    source: UpdateSource,
    temporary: Path,
    *,
    expected_size: int,
    deadline: float,
    progress_callback: DownloadProgress | None,
    opener: Callable[..., Any],
) -> None:
    offset = temporary.stat().st_size if temporary.is_file() else 0
    if offset > expected_size:
        temporary.unlink(missing_ok=True)
        offset = 0
    headers = {"User-Agent": "LiveClipper/4"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    request = urllib.request.Request(source.url, headers=headers)
    with _open_url(opener, request, DOWNLOAD_SOCKET_TIMEOUT) as response:
        final_url = str(response.geturl() if callable(getattr(response, "geturl", None)) else source.url)
        _validate_https_url(final_url, label="download redirect")
        status = int(getattr(response, "status", 0) or response.getcode() or 200)
        if offset and status == 206:
            content_range = str(response.headers.get("Content-Range") or "")
            match = re.match(r"bytes\s+(\d+)-\d+/(?:\d+|\*)", content_range, re.I)
            if not match or int(match.group(1)) != offset:
                raise UpdateChannelError("Runtime V4 download returned an invalid resume offset")
            mode = "ab"
            downloaded = offset
        else:
            mode = "wb"
            downloaded = 0
        _emit_progress(progress_callback, downloaded, expected_size, f"downloading from {source.name}")
        with temporary.open(mode) as handle:
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Runtime V4 download exceeded its total timeout")
                chunk = response.read(DOWNLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                handle.write(chunk)
                downloaded += len(chunk)
                if downloaded > expected_size:
                    raise UpdateChannelError("Runtime V4 download exceeds the signed size")
                _emit_progress(
                    progress_callback,
                    downloaded,
                    expected_size,
                    f"downloading from {source.name}",
                )


def download_business_bundle(
    channel: VerifiedUpdateChannel,
    destination: str | Path,
    progress_callback: DownloadProgress | None = None,
    *,
    total_timeout: int = DOWNLOAD_TOTAL_TIMEOUT,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> Path:
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file():
        if destination.stat().st_size == channel.size and _sha256_file(destination) == channel.sha256:
            _emit_progress(progress_callback, channel.size, channel.size, "using verified download cache")
            return destination
        destination.unlink(missing_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    if temporary.is_file() and temporary.stat().st_size > channel.size:
        temporary.unlink(missing_ok=True)
    if temporary.is_file() and temporary.stat().st_size == channel.size:
        if _sha256_file(temporary) == channel.sha256:
            os.replace(temporary, destination)
            return destination
        temporary.unlink(missing_ok=True)

    deadline = time.monotonic() + max(1, int(total_timeout))
    errors: list[str] = []
    for source in channel.sources:
        for attempt in range(1, DOWNLOAD_ATTEMPTS_PER_SOURCE + 1):
            if time.monotonic() >= deadline:
                errors.append("download exceeded its total timeout")
                break
            try:
                _download_from_source(
                    source,
                    temporary,
                    expected_size=channel.size,
                    deadline=deadline,
                    progress_callback=progress_callback,
                    opener=opener,
                )
                current_size = temporary.stat().st_size if temporary.is_file() else 0
                if current_size != channel.size:
                    raise UpdateChannelError(
                        f"Runtime V4 download is incomplete: {current_size}/{channel.size}"
                    )
                if _sha256_file(temporary) != channel.sha256:
                    temporary.unlink(missing_ok=True)
                    raise UpdateChannelError("Runtime V4 download SHA256 mismatch")
                os.replace(temporary, destination)
                _emit_progress(progress_callback, channel.size, channel.size, "download verified")
                return destination
            except Exception as exc:
                errors.append(f"{source.name} attempt {attempt}: {_safe_error(exc)}")
                if temporary.is_file() and temporary.stat().st_size > channel.size:
                    temporary.unlink(missing_ok=True)
    raise UpdateChannelError("Runtime V4 business download failed: " + "; ".join(errors[-6:]))


def fetch_signed_update_channel(
    urls: Iterable[str],
    public_key_path: str | Path,
    *,
    timeout: int = 15,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> VerifiedUpdateChannel:
    errors: list[str] = []
    seen: set[str] = set()
    for raw_url in urls:
        try:
            url = _validate_https_url(raw_url, label="channel source")
            if url in seen:
                continue
            seen.add(url)
            request = urllib.request.Request(url, headers={"User-Agent": "LiveClipper/4"})
            with _open_url(opener, request, max(1, int(timeout))) as response:
                final_url = str(response.geturl() if callable(getattr(response, "geturl", None)) else url)
                _validate_https_url(final_url, label="channel redirect")
                payload = response.read(MAX_CHANNEL_BYTES + 1)
            return load_update_channel_bytes(payload, public_key_path)
        except Exception as exc:
            errors.append(_safe_error(exc))
    if not seen and not errors:
        raise UpdateChannelError("Runtime V4 has no configured update channel sources")
    raise UpdateChannelError("Runtime V4 update channel fetch failed: " + "; ".join(errors[-4:]))


def apply_signed_business_update(
    install_root: str | Path,
    channel: VerifiedUpdateChannel,
    download_root: str | Path,
    *,
    public_key_path: str | Path,
    activate: bool = True,
    progress_callback: DownloadProgress | None = None,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> BusinessUpdateApplyResult:
    from runtime_v4.update_agent import _load_state, install_business_archive

    install_root = Path(install_root).resolve()
    _, _, current = _load_state(install_root)
    decision = plan_business_update(
        channel,
        current_version=current.application_version,
        current_core_version=current.core_version,
    )
    if not decision.available:
        return BusinessUpdateApplyResult(decision, None, None)
    archive_path = (
        Path(download_root).resolve()
        / channel.application_version
        / channel.filename
    )
    download_business_bundle(
        channel,
        archive_path,
        progress_callback,
        opener=opener,
    )
    installed = install_business_archive(
        install_root,
        archive_path,
        application_version=channel.application_version,
        public_key_path=public_key_path,
        activate=activate,
        expected_current_version=decision.current_version,
        expected_core_version=decision.current_core_version,
        expected_manifest_sha256=channel.bundle_manifest_sha256,
    )
    return BusinessUpdateApplyResult(decision, archive_path, installed)


__all__ = [
    "BusinessUpdateApplyResult",
    "BusinessUpdateDecision",
    "UpdateChannelError",
    "UpdateSource",
    "VerifiedUpdateChannel",
    "apply_signed_business_update",
    "build_signed_update_channel",
    "download_business_bundle",
    "fetch_signed_update_channel",
    "load_update_channel_bytes",
    "plan_business_update",
    "verify_update_channel",
]
