from __future__ import annotations

import json
import os
import re
from typing import Any


RUNTIME_PROFILE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
RUNTIME_IMAGE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,254}$")
MAX_RUNTIME_PROFILES = 20


def parse_runtime_image(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Runtime profile {label} image must be a string.")
    image = value.strip()
    if not RUNTIME_IMAGE_PATTERN.fullmatch(image):
        raise ValueError(f"Runtime profile {label} image is invalid.")
    return image


def get_runtime_profiles() -> dict[str, dict[str, str]]:
    profiles = {
        "standard": {
            "id": "standard",
            "label": "Standard",
            "image": parse_runtime_image(
                os.getenv("ANYDATAS_RUNTIME_IMAGE", "anydatas-runtime:latest"),
                "standard",
            ),
        }
    }
    raw_value = os.getenv("ANYDATAS_RUNTIME_PROFILES_JSON", "").strip()
    if not raw_value:
        return profiles
    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise ValueError("ANYDATAS_RUNTIME_PROFILES_JSON must be a JSON object.") from exc
    if not isinstance(payload, dict):
        raise ValueError("ANYDATAS_RUNTIME_PROFILES_JSON must be a JSON object.")
    if len(payload) > MAX_RUNTIME_PROFILES - 1:
        raise ValueError(f"At most {MAX_RUNTIME_PROFILES - 1} custom runtime profiles are allowed.")

    for profile_id, config in payload.items():
        if not isinstance(profile_id, str) or not RUNTIME_PROFILE_ID_PATTERN.fullmatch(profile_id):
            raise ValueError("Runtime profile ids must start with a lowercase letter and use lowercase letters, numbers, _ or -.")
        if profile_id == "standard":
            raise ValueError("The standard runtime profile cannot be overridden.")
        if not isinstance(config, dict) or set(config) != {"label", "image"}:
            raise ValueError(f"Runtime profile {profile_id} must contain exactly label and image.")
        label = config["label"]
        if not isinstance(label, str) or not label.strip() or len(label.strip()) > 64:
            raise ValueError(f"Runtime profile {profile_id} label must contain 1-64 characters.")
        profiles[profile_id] = {
            "id": profile_id,
            "label": label.strip(),
            "image": parse_runtime_image(config["image"], profile_id),
        }
    return profiles


def normalize_runtime_profile(value: str) -> str:
    profile_id = value.strip()
    if profile_id not in get_runtime_profiles():
        raise ValueError("Select an available runtime profile.")
    return profile_id


def runtime_profile_for_project(project: Any) -> dict[str, str]:
    try:
        profile_id = project["runtime_profile"] or "standard"
    except (KeyError, IndexError, TypeError):
        profile_id = "standard"
    profiles = get_runtime_profiles()
    if profile_id not in profiles:
        raise ValueError(f"Runtime profile is no longer configured: {profile_id}")
    return profiles[profile_id]
