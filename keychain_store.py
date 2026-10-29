"""Secure, full-length terminal input and macOS Keychain storage.

Passwords are never passed in process arguments or written to files. Apple's
SecurityTool password prompt uses getpass(3), which can truncate long input;
Python's getpass reads the full line, and SecurityTool receives hex data over
stdin instead. This module never prints captured output from SecurityTool.
"""
from __future__ import annotations

import getpass
import hmac
import os
from pathlib import Path
import shlex
import subprocess
import sys
import warnings


class SecretInputError(ValueError):
    pass


def _run(arguments, **kwargs):
    try:
        return subprocess.run(arguments, capture_output=True, text=True,
                              timeout=30, check=False, **kwargs)
    except (OSError, subprocess.SubprocessError):
        raise SecretInputError("macOS Keychain could not complete the operation. Check any local authorization prompt and retry.") from None


def _quoted(value):
    if not isinstance(value, str) or not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise SecretInputError("Invalid Keychain item identifier.")
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def default_keychain():
    result = _run(["/usr/bin/security", "default-keychain", "-d", "user"])
    try:
        paths = shlex.split(result.stdout.strip())
        path = Path(paths[0]) if len(paths) == 1 else None
        valid = result.returncode == 0 and path is not None and path.is_absolute() and path.is_file()
        if valid:
            valid = path.stat().st_uid == os.getuid()
    except (OSError, ValueError):
        valid = False
    if not valid:
        raise SecretInputError("The current user's default Keychain could not be verified. No secret was saved.")
    return path


def read_secret(service, account, keychain):
    result = _run(["/usr/bin/security", "find-generic-password", "-s", service,
                   "-a", account, "-w", str(keychain)])
    if result.returncode:
        raise SecretInputError("The saved Keychain item could not be read back. No service was started.")
    return result.stdout.rstrip("\n")


def prompt_and_store(service, account, label, *, validator=None):
    """Have the local user enter a secret, then verify the stored value exactly."""
    if sys.platform != "darwin" or not sys.stdin.isatty():
        raise SecretInputError("Enter credentials in your own interactive Mac terminal. Never paste them into chat.")
    _quoted(service)
    _quoted(account)
    keychain = default_keychain()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            secret = getpass.getpass(label + ": ")
            confirmation = getpass.getpass("Repeat " + label.lower() + ": ")
    except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        raise SecretInputError("Secure credential entry was cancelled or unavailable. No secret was saved.") from None
    if (not secret or len(secret) > 512 or any(ord(c) < 32 or ord(c) == 127 for c in secret)
            or not hmac.compare_digest(secret.encode(), confirmation.encode())
            or (validator is not None and not validator(secret))):
        raise SecretInputError("The credential must be 1 to 512 characters, contain no control characters, and match its confirmation. No secret was saved.")
    # -X avoids command-parser interpretation of characters in passwords. Only
    # the nonsecret security executable and -i appear in the process arguments.
    command = ("add-generic-password -U -s " + _quoted(service) + " -a " + _quoted(account)
               + " -T /usr/bin/security -X " + secret.encode("utf-8").hex()
               + " " + _quoted(str(keychain)) + "\n")
    if len(command.encode("utf-8")) > 2048:
        raise SecretInputError("The Keychain command exceeds the supported safe input size. No secret was saved.")
    result = _run(["/usr/bin/security", "-i"], input=command)
    if result.returncode:
        raise SecretInputError("The credential was not saved. Check any local Keychain authorization prompt and retry.")
    saved = read_secret(service, account, keychain)
    if not hmac.compare_digest(secret.encode(), saved.encode()):
        raise SecretInputError("The complete saved credential could not be verified. No service was started.")
    return saved
