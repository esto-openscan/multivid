from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class RecordingProfile:
    name: str
    description: str
    output_extension: str
    rpicam_vid_args: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "output_extension": self.output_extension,
            "rpicam_vid_args": self.rpicam_vid_args,
        }


class RecordingProfiles:
    def __init__(self, profiles: dict[str, RecordingProfile]) -> None:
        self._profiles = profiles

    def get(self, name: str) -> RecordingProfile | None:
        return self._profiles.get(name)

    def names(self) -> list[str]:
        return sorted(self._profiles.keys())

    def as_dict(self) -> dict[str, dict[str, Any]]:
        return {name: profile.as_dict() for name, profile in sorted(self._profiles.items())}


def load_recording_profiles(path: str | Path) -> RecordingProfiles:
    profiles_path = Path(path)
    with profiles_path.open("r", encoding="utf-8") as profiles_file:
        data = yaml.safe_load(profiles_file) or {}

    if not isinstance(data, dict):
        raise ValueError(f"{profiles_path}: expected a YAML mapping")

    raw_profiles = data.get("profiles", data)
    if not isinstance(raw_profiles, dict):
        raise ValueError(f"{profiles_path}: profiles must be a YAML mapping")

    profiles: dict[str, RecordingProfile] = {}
    for name, raw_profile in raw_profiles.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{profiles_path}: profile names must be non-empty strings")
        if not isinstance(raw_profile, dict):
            raise ValueError(f"{profiles_path}: profile {name!r} must be a mapping")

        raw_args = raw_profile.get("rpicam_vid_args", [])
        if not isinstance(raw_args, list) or not all(isinstance(item, (str, int, float)) for item in raw_args):
            raise ValueError(f"{profiles_path}: profile {name!r} rpicam_vid_args must be a list of scalar values")

        output_extension = str(raw_profile.get("output_extension", "h264")).lstrip(".")
        profiles[name] = RecordingProfile(
            name=name,
            description=str(raw_profile.get("description", "")),
            output_extension=output_extension or "h264",
            rpicam_vid_args=[str(item) for item in raw_args],
        )

    if not profiles:
        raise ValueError(f"{profiles_path}: at least one recording profile is required")

    return RecordingProfiles(profiles)
