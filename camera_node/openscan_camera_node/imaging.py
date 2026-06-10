from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .profiles import RecordingProfile


JPEG_BACKEND_CANDIDATES = ("rpicam-still", "rpicam-jpeg")
POSITIONING_BACKEND_NAME = "rpicam-still/jpeg"


class JpegCaptureError(Exception):
    """Raised when a still-image backend command fails."""


@dataclass(frozen=True)
class JpegCaptureResult:
    image_bytes: bytes | None
    output_file: Path | None
    backend: str
    command: list[str]
    warnings: list[str]
    stderr: str
    exit_code: int


class RpicamJpegBackend:
    def capture_to_bytes(
        self,
        *,
        width: int | None,
        height: int | None,
        quality: int,
        camera_controls: dict[str, Any] | None = None,
        camera_transform: dict[str, Any] | None = None,
        overlay_settings: dict[str, Any] | None = None,
        timeout_ms: int = 350,
    ) -> JpegCaptureResult:
        with tempfile.TemporaryDirectory(prefix="openscan-positioning-") as temp_dir:
            output_file = Path(temp_dir) / "snapshot.jpg"
            result = self.capture_to_file(
                output_file=output_file,
                width=width,
                height=height,
                quality=quality,
                camera_controls=camera_controls,
                camera_transform=camera_transform,
                timeout_ms=timeout_ms,
            )
            image_bytes = output_file.read_bytes()
            warnings = list(result.warnings)
            if overlay_settings:
                image_bytes, overlay_warnings = render_positioning_overlays(
                    image_bytes,
                    camera_id=str(overlay_settings.get("camera_id") or ""),
                    overlays=overlay_settings.get("overlays") or [],
                    jpeg_quality=quality,
                )
                warnings.extend(overlay_warnings)
            return JpegCaptureResult(
                image_bytes=image_bytes,
                output_file=None,
                backend=result.backend,
                command=result.command,
                warnings=_dedupe(warnings),
                stderr=result.stderr,
                exit_code=result.exit_code,
            )

    def capture_to_file(
        self,
        *,
        output_file: Path,
        width: int | None,
        height: int | None,
        quality: int,
        camera_controls: dict[str, Any] | None = None,
        camera_transform: dict[str, Any] | None = None,
        timeout_ms: int = 1000,
    ) -> JpegCaptureResult:
        backend = resolve_jpeg_backend()
        command = build_jpeg_capture_command(
            backend=backend,
            output_file=output_file,
            width=width,
            height=height,
            quality=quality,
            camera_controls=camera_controls or {},
            camera_transform=camera_transform or {},
            timeout_ms=timeout_ms,
        )
        output_file.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            cwd=output_file.parent,
            timeout=max(10.0, timeout_ms / 1000.0 + 8.0),
            check=False,
            text=True,
        )
        stderr = result.stderr or ""
        if result.returncode != 0:
            detail = _last_stderr_line(stderr) or f"exit code {result.returncode}"
            raise JpegCaptureError(f"{backend} failed: {detail}")
        if not output_file.exists() or output_file.stat().st_size <= 0:
            raise JpegCaptureError(f"{backend} did not produce a non-empty JPEG")
        return JpegCaptureResult(
            image_bytes=None,
            output_file=output_file,
            backend=backend,
            command=command,
            warnings=[],
            stderr=stderr,
            exit_code=result.returncode,
        )


def resolve_jpeg_backend() -> str:
    for candidate in JPEG_BACKEND_CANDIDATES:
        if shutil.which(candidate):
            return candidate
    names = " or ".join(JPEG_BACKEND_CANDIDATES)
    raise FileNotFoundError(f"{names} was not found on this node")


def build_jpeg_capture_command(
    *,
    backend: str,
    output_file: Path,
    width: int | None,
    height: int | None,
    quality: int,
    camera_controls: dict[str, Any],
    camera_transform: dict[str, Any],
    timeout_ms: int,
) -> list[str]:
    command = [
        backend,
        "--output",
        str(output_file),
        "--timeout",
        str(max(1, int(timeout_ms))),
        "--quality",
        str(quality),
        "--nopreview",
    ]
    if width is not None:
        command.extend(["--width", str(width)])
    if height is not None:
        command.extend(["--height", str(height)])
    _extend_camera_control_args(command, camera_controls)
    _extend_camera_transform_args(command, camera_transform)
    return command


def camera_controls_from_profile(profile: RecordingProfile | None) -> dict[str, Any]:
    if profile is None:
        return {}
    controls = profile.camera_controls
    result: dict[str, Any] = {}
    for key in (
        "shutter_us",
        "gain",
        "awbgains",
        "autofocus_mode",
        "lens_position",
        "denoise",
        "ev",
        "metering",
        "awb",
    ):
        value = controls.get(key)
        if _has_value(value):
            result[key] = value
    return result


def render_positioning_overlays(
    jpeg_bytes: bytes,
    *,
    camera_id: str,
    overlays: list[str],
    jpeg_quality: int,
) -> tuple[bytes, list[str]]:
    if not overlays:
        return jpeg_bytes, []
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return jpeg_bytes, ["Pillow is not installed; requested positioning overlays were not rendered"]

    try:
        image = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
        draw = ImageDraw.Draw(image, "RGBA")
        width, height = image.size
        if "grid" in overlays:
            _draw_grid(draw, width, height)
        if "shorts_safe_area" in overlays:
            _draw_shorts_safe_area(draw, width, height)
        if "crosshair" in overlays:
            _draw_crosshair(draw, width, height)
        if "camera_label" in overlays:
            font = ImageFont.load_default()
            _draw_camera_label(draw, camera_id, font)

        output = io.BytesIO()
        image.save(output, format="JPEG", quality=jpeg_quality)
        return output.getvalue(), []
    except Exception as exc:
        return jpeg_bytes, [f"failed to render positioning overlays: {exc}"]


def _extend_camera_control_args(command: list[str], controls: dict[str, Any]) -> None:
    _add_value_arg(command, "--shutter", controls.get("shutter_us"))
    _add_value_arg(command, "--gain", controls.get("gain"))
    awbgains = controls.get("awbgains")
    if isinstance(awbgains, list) and len(awbgains) == 2:
        command.extend(["--awbgains", ",".join(str(item) for item in awbgains)])
    _add_value_arg(command, "--autofocus-mode", controls.get("autofocus_mode"))
    _add_value_arg(command, "--lens-position", controls.get("lens_position"))
    _add_value_arg(command, "--denoise", controls.get("denoise"))
    _add_value_arg(command, "--ev", controls.get("ev"))
    _add_value_arg(command, "--metering", controls.get("metering"))
    _add_value_arg(command, "--awb", controls.get("awb"))


def _extend_camera_transform_args(command: list[str], transform: dict[str, Any]) -> None:
    _add_flag_arg(command, "--hflip", transform.get("hflip"))
    _add_flag_arg(command, "--vflip", transform.get("vflip"))


def _add_value_arg(command: list[str], option: str, value: Any) -> None:
    if not _has_value(value):
        return
    command.extend([option, str(value)])


def _add_flag_arg(command: list[str], option: str, value: Any) -> None:
    if value is True:
        command.append(option)


def _draw_grid(draw: Any, width: int, height: int) -> None:
    minor_color = (255, 255, 255, 135)
    major_color = (255, 245, 90, 190)
    shadow = (0, 0, 0, 150)
    for index in range(1, 6):
        is_third = index in {2, 4}
        color = major_color if is_third else minor_color
        line_width = 2 if is_third else 1
        x = width * index / 6
        y = height * index / 6
        _line(draw, (x, 0, x, height), color, shadow, width=line_width)
        _line(draw, (0, y, width, y), color, shadow, width=line_width)


def _draw_crosshair(draw: Any, width: int, height: int) -> None:
    cx = width / 2
    cy = height / 2
    length = max(18, min(width, height) * 0.08)
    color = (255, 245, 90, 210)
    shadow = (0, 0, 0, 140)
    _line(draw, (cx - length, cy, cx + length, cy), color, shadow, width=2)
    _line(draw, (cx, cy - length, cx, cy + length), color, shadow, width=2)


def _draw_shorts_safe_area(draw: Any, width: int, height: int) -> None:
    safe_width = min(width, height * 9 / 16)
    left = (width - safe_width) / 2
    right = left + safe_width
    color = (80, 220, 255, 185)
    shadow = (0, 0, 0, 150)
    for inset in (0, 1):
        draw.rectangle(
            (left + inset, inset, right - inset, height - 1 - inset),
            outline=shadow,
            width=1,
        )
    draw.rectangle((left, 0, right, height - 1), outline=color, width=2)


def _draw_camera_label(draw: Any, camera_id: str, font: Any) -> None:
    label = camera_id or "camera"
    bbox = draw.textbbox((0, 0), label, font=font)
    padding = 6
    rect = (
        8,
        8,
        8 + (bbox[2] - bbox[0]) + padding * 2,
        8 + (bbox[3] - bbox[1]) + padding * 2,
    )
    draw.rectangle(rect, fill=(0, 0, 0, 145))
    draw.text((8 + padding, 8 + padding), label, font=font, fill=(255, 255, 255, 230))


def _line(draw: Any, coords: tuple[float, float, float, float], color: tuple[int, int, int, int], shadow: tuple[int, int, int, int], width: int = 1) -> None:
    sx1, sy1, sx2, sy2 = coords
    draw.line((sx1 + 1, sy1 + 1, sx2 + 1, sy2 + 1), fill=shadow, width=width + 1)
    draw.line(coords, fill=color, width=width)


def _has_value(value: Any) -> bool:
    return value is not None and value is not False


def _last_stderr_line(stderr: str) -> str | None:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    return lines[-1] if lines else None


def _dedupe(values: list[str]) -> list[str]:
    deduped: list[str] = []
    for value in values:
        if value not in deduped:
            deduped.append(value)
    return deduped
