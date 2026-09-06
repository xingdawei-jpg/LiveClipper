"""Lossless compact wire format for the Director Beat Casting response.

The provider returns this format only at the network boundary.  The rest of
the Director pipeline receives the established, explicit dictionary shape, so
validation and preview materialisation do not acquire a second contract.
"""

from __future__ import annotations

import copy
from typing import Any, Mapping


WIRE_VERSION = "director-wire-v1"
_ENCODE_ALIASES = {
    "beat_function": "role",
    "subtitle_ids": "ids",
    "source_seconds": "sec",
    "product_relation": "rel",
    "product_evidence_ids": "evidence",
    "supports_main_product": "support",
    "replaces_beat_id": "replaces",
}
_DECODE_ALIASES = {value: key for key, value in _ENCODE_ALIASES.items()}


def _is_beat(value: Mapping[str, Any]) -> bool:
    return "beat_function" in value and "subtitle_ids" in value


def _product_ref(
    subject_product: Any,
    subject_product_type: Any,
    products: list[dict[str, str]],
) -> int | None:
    name = str(subject_product or "").strip()
    kind = str(subject_product_type or "").strip()
    if not name and not kind:
        return None
    if not name or not kind:
        return None
    item = {"name": name, "type": kind}
    try:
        return products.index(item)
    except ValueError:
        products.append(item)
        return len(products) - 1


def _encode(value: Any, products: list[dict[str, str]]) -> Any:
    if isinstance(value, list):
        return [_encode(item, products) for item in value]
    if not isinstance(value, Mapping):
        return copy.deepcopy(value)
    result = {str(key): _encode(item, products) for key, item in value.items()}
    if not _is_beat(value):
        return result
    for original, compact in _ENCODE_ALIASES.items():
        if original in result:
            result[compact] = result.pop(original)
    ref = _product_ref(value.get("subject_product"), value.get("subject_product_type"), products)
    if ref is not None:
        result.pop("subject_product", None)
        result.pop("subject_product_type", None)
        result["product_ref"] = ref
    return result


def compact_director_wire_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Encode a normal Casting payload without selecting, dropping or editing facts."""
    products: list[dict[str, str]] = []
    return {
        "schema_version": WIRE_VERSION,
        "products": products,
        "packet": _encode(payload, products),
    }


def _decode(value: Any, products: list[dict[str, str]]) -> Any:
    if isinstance(value, list):
        return [_decode(item, products) for item in value]
    if not isinstance(value, Mapping):
        return copy.deepcopy(value)
    result = {str(key): _decode(item, products) for key, item in value.items()}
    is_compact_beat = "role" in result and "ids" in result
    if not is_compact_beat:
        return result
    for compact, original in _DECODE_ALIASES.items():
        if compact in result:
            result[original] = result.pop(compact)
    if "product_ref" in result:
        ref = result.pop("product_ref")
        if not isinstance(ref, int) or isinstance(ref, bool) or not 0 <= ref < len(products):
            raise ValueError("Director wire product_ref is invalid")
        product = products[ref]
        result["subject_product"] = product["name"]
        result["subject_product_type"] = product["type"]
    return result


def expand_director_wire_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Strictly validate and expand a Director wire packet to the legacy shape."""
    if str(payload.get("schema_version") or "") != WIRE_VERSION:
        raise ValueError("Unsupported Director wire schema")
    raw_products = payload.get("products")
    packet = payload.get("packet")
    if not isinstance(raw_products, list) or not isinstance(packet, Mapping):
        raise ValueError("Director wire packet is incomplete")
    products: list[dict[str, str]] = []
    for raw in raw_products:
        if not isinstance(raw, Mapping):
            raise ValueError("Director wire product entry is invalid")
        name = str(raw.get("name") or "").strip()
        kind = str(raw.get("type") or "").strip()
        if not name or not kind:
            raise ValueError("Director wire product entry is incomplete")
        products.append({"name": name, "type": kind})
    return _decode(packet, products)
