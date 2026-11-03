"""Stateless reader for installed role templates.

Provides single-source loading of installed role HTML templates from STATIC_DIR,
with explicit validation and controlled 503 error handling.
"""
from pathlib import Path
from typing import Dict

ROLE_FILES: Dict[str, str] = {
    "login": "login.html",
    "admin": "admin.html",
    "family": "family.html",
}


class RoleTemplateUnavailable(Exception):
    """Raised when a required role template is missing, unreadable, or not a regular file."""
    pass


def read_role_template(static_dir: str, role: str) -> bytes:
    """Read raw bytes of the requested role template from static_dir.

    Args:
        static_dir: Directory containing installed web assets (e.g. STATIC_DIR).
        role: Key corresponding to known role page ('login', 'admin', 'family').

    Returns:
        Raw bytes of the template file without interpolation or decoding.

    Raises:
        ValueError: If role is unknown.
        RoleTemplateUnavailable: If static_dir does not exist or template file is
            missing, unreadable, or not a regular file.
    """
    if role not in ROLE_FILES:
        raise ValueError(f"Unknown role template: {role!r}")

    filename = ROLE_FILES[role]
    dir_path = Path(static_dir)
    target_path = dir_path / filename

    try:
        if not target_path.is_file():
            raise RoleTemplateUnavailable(
                f"Role template {filename!r} is not a regular file in {static_dir}"
            )
        return target_path.read_bytes()
    except OSError as err:
        raise RoleTemplateUnavailable(
            f"Failed to read role template {filename!r} from {static_dir}: {err}"
        ) from err
