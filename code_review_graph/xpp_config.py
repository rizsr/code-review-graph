"""User-scoped configuration for X++ / D365 metadata support."""

from __future__ import annotations

import json
import os
from pathlib import Path

_CONFIG_DIR = Path.home() / ".code-review-graph"
_CONFIG_PATH = _CONFIG_DIR / "config.json"
_AUTO_XPP_ROOTS = [
    Path.home() / "AppData" / "Local" / "Microsoft" / "Dynamics365",
]


def load_user_config() -> dict:
    """Load the user-scoped CRG config file."""
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_user_config(config: dict) -> None:
    """Persist the user-scoped CRG config file."""
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def get_xpp_base_roots() -> list[str]:
    """Return configured X++ metadata roots, falling back to env/autodetect."""
    env_value = os.environ.get("CRG_XPP_BASE_ROOTS", "").strip()
    if env_value:
        return [
            str(Path(part).expanduser().resolve())
            for part in env_value.split(os.pathsep)
            if part.strip()
        ]

    config = load_user_config()
    configured = config.get("xpp_base_roots", [])
    if isinstance(configured, list):
        roots = [
            str(Path(part).expanduser().resolve())
            for part in configured
            if isinstance(part, str) and part.strip()
        ]
        if roots:
            return roots

    detected: list[str] = []
    for base in _AUTO_XPP_ROOTS:
        if not base.is_dir():
            continue
        for child in base.iterdir():
            packages = child / "PackagesLocalDirectory"
            if packages.is_dir():
                detected.append(str(packages.resolve()))
    return detected


def set_xpp_base_roots(roots: list[str]) -> list[str]:
    """Persist X++ metadata roots and return their normalized values."""
    normalized = [
        str(Path(root).expanduser().resolve())
        for root in roots
        if root and str(root).strip()
    ]
    config = load_user_config()
    config["xpp_base_roots"] = normalized
    save_user_config(config)
    return normalized
