"""Load the project credential without depending on the Host working directory."""

from __future__ import annotations

import os
import re
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path

API_KEY_ENV = "ZHENZHEN_GPTIMAGEGEN_API_KEY"
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class ConfigError(ValueError):
    """Configuration is absent, invalid, or unreadable; details exclude values."""


@dataclass(frozen=True)
class Config:
    api_key: str = field(repr=False)


def project_root() -> Path:
    """Return the project root relative to the installed editable source module."""
    return Path(__file__).resolve().parents[2]


def _parse_env(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    names: set[str] = set()
    for number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, raw = line.partition("=")
        name = name.strip()
        if not separator or not _NAME.fullmatch(name):
            raise ConfigError(f"Invalid .env syntax at line {number}")
        normalized_name = name.upper()
        if normalized_name in names:
            raise ConfigError(f"Duplicate .env field at line {number}")
        names.add(normalized_name)
        try:
            words = shlex.split(raw, comments=True, posix=True)
        except ValueError:
            raise ConfigError(f"Invalid .env value at line {number}") from None
        if len(words) > 1:
            raise ConfigError(f"Invalid .env value at line {number}")
        result[normalized_name] = words[0] if words else ""
    return result


def _read_windows_user_value(name: str) -> str | None:
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, kind = winreg.QueryValueEx(key, name)
    except FileNotFoundError:
        return None
    if kind != winreg.REG_SZ or not isinstance(value, str):
        raise ConfigError("Invalid Windows user environment value")
    return value


def _validated(value: str) -> str:
    if not value or not value.strip() or any(char.isspace() for char in value):
        raise ConfigError(f"{API_KEY_ENV} is empty or invalid")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ConfigError(f"{API_KEY_ENV} is empty or invalid")
    return value


def load_config(root: Path | None = None) -> Config:
    """Read project .env, process, then current-user environment; only missing falls back."""
    env_path = (root if root is not None else project_root()) / ".env"
    try:
        text = env_path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        values: dict[str, str] = {}
    except (OSError, UnicodeError):
        raise ConfigError("Cannot read project .env") from None
    else:
        values = _parse_env(text)
    if API_KEY_ENV in values:
        return Config(_validated(values[API_KEY_ENV]))
    if API_KEY_ENV in os.environ:
        return Config(_validated(os.environ[API_KEY_ENV]))
    try:
        value = _read_windows_user_value(API_KEY_ENV)
    except (OSError, ValueError):
        raise ConfigError("Cannot read Windows user environment") from None
    if value is None:
        raise ConfigError(f"{API_KEY_ENV} is missing")
    return Config(_validated(value))
