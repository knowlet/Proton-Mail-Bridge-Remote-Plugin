"""Nonsecret, per-user configuration for the local Proton Mail Bridge plugin."""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

KEYCHAIN_SERVICE = "proton_mail_bridge_plugin"
MODES = ("read-only", "drafts", "send")
ALLOWED_KEYS = {"username", "imap_port", "smtp_port", "cert_path", "mode"}


class ConfigError(ValueError):
    pass


def config_path() -> Path:
    raw = os.environ.get("PROTON_MAIL_BRIDGE_CONFIG")
    path = Path(raw).expanduser() if raw else Path.home() / ".config/proton-mail-bridge-plugin/config.json"
    if not path.is_absolute():
        raise ConfigError("The configuration path must be absolute.")
    return path


def validate_config(data: dict) -> dict:
    if not isinstance(data, dict) or set(data) - ALLOWED_KEYS:
        raise ConfigError("Unsupported configuration fields; passwords do not belong in this file.")
    user = data.get("username")
    if not isinstance(user, str) or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9][A-Za-z0-9.-]*", user) or len(user) > 320:
        raise ConfigError("Enter the Bridge username as a plain ASCII email address, without a display name.")
    mode = data.get("mode", "read-only")
    if mode not in MODES:
        raise ConfigError("Mode must be read-only, drafts, or send.")
    result = {"username": user, "mode": mode}
    for key, default in (("imap_port", 1143), ("smtp_port", 1025)):
        port = data.get(key, default)
        if type(port) is not int or not 1 <= port <= 65535:
            raise ConfigError("Bridge ports must be integers from 1 to 65535.")
        result[key] = port
    cert = data.get("cert_path")
    if not isinstance(cert, str) or not cert or any(ord(c) < 32 or ord(c) == 127 for c in cert):
        raise ConfigError("An absolute path to the exported Bridge certificate is required.")
    if not Path(cert).is_absolute():
        raise ConfigError("The certificate path must be absolute.")
    result["cert_path"] = cert
    return result


def _private_regular_file(path: Path) -> None:
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077:
        raise ConfigError("Configuration must be an owner-only regular file (chmod 600), not a symlink.")
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        raise ConfigError("Configuration must belong to the current user.")
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_mode & 0o022:
        raise ConfigError("Configuration directory must not be a symlink or writable by other users.")
    if hasattr(os, "getuid") and parent.st_uid != os.getuid():
        raise ConfigError("Configuration directory must belong to the current user.")


def load_config(path: Path | str | None = None) -> dict | None:
    path = Path(path) if path is not None else config_path()
    if not path.is_absolute():
        raise ConfigError("The configuration path must be absolute.")
    try:
        _private_regular_file(path)
    except FileNotFoundError:
        return None
    try:
        if path.stat().st_size > 16_384:
            raise ConfigError("Configuration is unexpectedly large.")
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError("Configuration could not be read as UTF-8 JSON.") from exc
    return validate_config(data)
