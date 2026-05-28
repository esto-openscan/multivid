from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SUGGESTED_CONTROL_FIELDS = ("shutter_us", "gain", "awbgains", "lens_position")

_FIELD_ALIASES = {
    "shutter_us": ("ExposureTime", "exposure_time", "exposure_us", "shutter_us", "shutter"),
    "gain": ("AnalogueGain", "AnalogGain", "analogue_gain", "analog_gain", "gain"),
    "awbgains": ("ColourGains", "ColorGains", "colour_gains", "color_gains", "AwbGains", "awbgains"),
    "lens_position": ("LensPosition", "lens_position", "FocusPosition", "focus_position"),
}
_TEXT_FIELD_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_ -]*)\s*[:=]\s*(.*?)\s*$")
_UNAVAILABLE_WARNING = "Not available from rpicam-vid metadata on this backend"


def parse_rpicam_metadata(path: Path) -> dict[str, Any]:
    if not path.exists() or path.stat().st_size == 0:
        return {"records": [], "warnings": ["metadata file was not produced or was empty"]}

    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return {"records": [], "warnings": ["metadata file was empty"]}

    records, warnings = _parse_json_metadata(text)
    if not records:
        records, text_warnings = _parse_text_metadata(text)
        warnings = text_warnings if records else [*warnings, *text_warnings]

    return {"records": records, "warnings": _dedupe(warnings)}


def build_suggested_controls(
    *,
    metadata_result: dict[str, Any],
    profile_name: str,
    profile_snapshot: dict[str, Any],
    camera_id: str,
    calibration_id: str,
    calibration_manifest_path: Path,
) -> dict[str, Any]:
    records = metadata_result.get("records")
    if not isinstance(records, list):
        records = []
    observed = _last_observed_values(records)

    warnings = list(metadata_result.get("warnings", []))
    suggested_controls: dict[str, dict[str, Any]] = {}
    unavailable_fields: list[str] = []
    for field in SUGGESTED_CONTROL_FIELDS:
        observed_value = observed.get(field)
        if observed_value is None:
            unavailable_fields.append(field)
            warning = _UNAVAILABLE_WARNING
            warnings.append(f"{field}: {warning}")
            suggested_controls[field] = {
                "value": None,
                "source_field": None,
                "warning": warning,
            }
        else:
            suggested_controls[field] = {
                "value": observed_value["value"],
                "source_field": observed_value["source_field"],
                "warning": None,
            }

    return {
        "source": "rpicam-vid metadata" if records else "fallback",
        "confidence": _confidence(suggested_controls),
        "suggested_controls": suggested_controls,
        "camera_controls_yaml": {
            key: item["value"]
            for key, item in suggested_controls.items()
            if item.get("value") is not None
        },
        "warnings": _dedupe(warnings),
        "unsupported": unavailable_fields,
        "unavailable_fields": unavailable_fields,
        "timestamp": _utc_now(),
        "profile": profile_name,
        "profile_snapshot": profile_snapshot,
        "camera_id": camera_id,
        "calibration_id": calibration_id,
        "calibration_manifest_path": str(calibration_manifest_path),
        "observed_frame_count": len(records),
    }


def suggestion_values(suggestions: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(suggestions, dict):
        return {}
    controls = suggestions.get("suggested_controls")
    if not isinstance(controls, dict):
        return {}
    values: dict[str, Any] = {}
    for key in SUGGESTED_CONTROL_FIELDS:
        item = controls.get(key)
        if isinstance(item, dict) and item.get("value") is not None:
            values[key] = item["value"]
    return values


def _parse_json_metadata(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None

    if parsed is not None:
        return _records_from_json_value(parsed), warnings

    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            warnings.append(f"metadata line {line_number} was not JSON")
            continue
        records.extend(_records_from_json_value(value))
    return records, warnings


def _records_from_json_value(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        for key in ("frames", "metadata", "records"):
            nested = value.get(key)
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _parse_text_metadata(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    record: dict[str, Any] = {}
    warnings: list[str] = []
    for line in text.splitlines():
        match = _TEXT_FIELD_RE.match(line)
        if not match:
            continue
        key = match.group(1).strip().replace(" ", "")
        record[key] = _parse_scalar_or_list(match.group(2).strip())
    if not record:
        warnings.append("metadata file could not be parsed as JSON or key/value text")
    return ([record] if record else []), warnings


def _parse_scalar_or_list(value: str) -> Any:
    stripped = value.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        parts = [part.strip() for part in stripped[1:-1].split(",") if part.strip()]
        parsed = [_parse_scalar(part) for part in parts]
        return parsed
    if "," in stripped:
        parts = [part.strip() for part in stripped.split(",") if part.strip()]
        if len(parts) == 2:
            return [_parse_scalar(part) for part in parts]
    return _parse_scalar(stripped)


def _parse_scalar(value: str) -> Any:
    try:
        number = float(value)
    except ValueError:
        return value
    return _clean_number(number)


def _last_observed_values(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    observed: dict[str, dict[str, Any]] = {}
    for record in records:
        for field, aliases in _FIELD_ALIASES.items():
            value, source_field = _first_present(record, aliases)
            normalized = _normalize_observed_value(field, value)
            if normalized is not None:
                observed[field] = {"value": normalized, "source_field": source_field}
    return observed


def _first_present(record: dict[str, Any], aliases: tuple[str, ...]) -> tuple[Any, str | None]:
    lower_map = {str(key).lower(): key for key in record.keys()}
    for alias in aliases:
        key = alias if alias in record else lower_map.get(alias.lower())
        if key is not None:
            return record.get(key), str(key)
    return None, None


def _normalize_observed_value(field: str, value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return None
    if field == "awbgains":
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            return None
        gains = [_number(item) for item in value]
        if gains[0] is None or gains[1] is None:
            return None
        return gains
    number = _number(value)
    if number is None:
        return None
    if field == "shutter_us":
        return int(round(float(number)))
    return number


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _clean_number(float(value))
    if isinstance(value, str):
        try:
            return _clean_number(float(value))
        except ValueError:
            return None
    return None


def _confidence(suggested_controls: dict[str, dict[str, Any]]) -> str:
    has_exposure = suggested_controls["shutter_us"]["value"] is not None
    has_gain = suggested_controls["gain"]["value"] is not None
    has_awb = suggested_controls["awbgains"]["value"] is not None
    has_focus = suggested_controls["lens_position"]["value"] is not None
    if has_exposure and has_gain and has_awb and has_focus:
        return "high"
    if has_exposure and has_gain and has_awb:
        return "medium"
    return "low"


def _clean_number(value: float) -> int | float:
    return int(value) if value.is_integer() else value


def _dedupe(values: list[str]) -> list[str]:
    deduped: list[str] = []
    for value in values:
        if value not in deduped:
            deduped.append(value)
    return deduped


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
