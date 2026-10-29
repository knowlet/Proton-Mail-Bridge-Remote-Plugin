#!/usr/bin/env python3
"""Interactive macOS setup. Never accepts or prints passwords as arguments."""
import argparse
import hashlib
import imaplib
import json
import os
import ssl
import stat
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

from keychain_store import prompt_and_store

ROOT = Path(__file__).resolve().parent


def private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = path.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise ValueError("The setup directory must be a real directory owned by you.")
    if st.st_mode & 0o077:
        os.chmod(path, 0o700)


def private_write(path, content):
    private_directory(path.parent)
    if path.is_symlink():
        raise ValueError("Refusing to replace a symlink.")
    fd, temp = tempfile.mkstemp(prefix=".setup-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def install_runtime():
    from run import runtime_path
    runtime = runtime_path()
    private_directory(runtime.parent)
    if runtime.is_symlink():
        raise ValueError("Refusing to use a symlink as the runtime directory.")
    python = runtime / "bin/python"
    if not python.exists():
        venv.EnvBuilder(with_pip=True).create(runtime)
    print("Installing the bundled, hash-verified Python dependencies…")
    subprocess.run([str(python), "-m", "pip", "--disable-pip-version-check", "install",
                    "--require-hashes", "-r", str(ROOT / "server/requirements.txt")], check=True)


def keychain_password(username):
    result = subprocess.run(["/usr/bin/security", "find-generic-password", "-s",
                             "proton_mail_bridge_plugin", "-a", username, "-w"],
                            capture_output=True, text=True, timeout=30)
    if result.returncode or not result.stdout.rstrip("\n"):
        raise ValueError("The Bridge password is not available from macOS Keychain.")
    return result.stdout.rstrip("\n")


def verify_connection(config):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_verify_locations(cafile=config["cert_path"])
    password = keychain_password(config["username"])
    client = None
    try:
        client = imaplib.IMAP4("127.0.0.1", config["imap_port"], timeout=10)
        client.starttls(ssl_context=ctx)
        client.login(config["username"], password)
        typ, _ = client.noop()
        if typ != "OK":
            raise ValueError("Bridge did not accept the connection check.")
    finally:
        password = None
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass
    print("IMAP login verified. No message was read, marked, drafted, or sent.")


def prompt(label, default=None):
    suffix = " [" + str(default) + "]" if default is not None else ""
    value = input(label + suffix + ": ").strip()
    return value or default


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check existing TLS/IMAP login without changing settings.")
    parser.add_argument("--mode", choices=("read-only", "drafts", "send"), help="Explicitly choose enabled mail tools; never sends a message.")
    parser.add_argument("--replace-password", action="store_true", help="Prompt securely in the terminal for a new Bridge password.")
    args = parser.parse_args()
    if sys.version_info < (3, 10):
        parser.error("Python 3.10 or newer is required. Install a current Python, then run setup again.")
    if sys.platform != "darwin":
        parser.error("This package uses macOS Keychain. Run setup on the Mac that runs Proton Mail Bridge.")
    sys.path.insert(0, str(ROOT / "server"))
    from configuration import config_path, load_config, validate_config
    path = config_path()
    old = load_config(path)
    if args.check:
        if args.mode or args.replace_password:
            parser.error("--check cannot change the mode or password.")
        if old is None:
            parser.error("No configuration yet. Run setup without --check first.")
        verify_connection(old)
        return 0
    if not sys.stdin.isatty():
        parser.error("Run setup in your own interactive Mac terminal; never paste credentials into chat.")
    print("Proton Mail Bridge — local plugin setup\n")
    print("Leave the official Bridge app signed in. Use its Mailbox details, not your Proton login password.")
    print("Connection mode: STARTTLS. Export the public TLS certificate from Bridge Settings → Advanced.")
    print("Mail returned by the tools is passed to your AI client. The Bridge password stays in Keychain.\n")
    if old and args.mode and not args.replace_password:
        current = dict(old, mode=args.mode)
        validate_config(current)
        verify_connection(current)
        private_write(path, json.dumps(current, ensure_ascii=False, indent=2) + "\n")
        print("Mode set to " + current["mode"] + ". Reopen the local MCP connection to reload its tools.")
        return 0
    current = dict(old or {})
    current["username"] = prompt("Bridge username", current.get("username"))
    current["imap_port"] = int(prompt("IMAP port", current.get("imap_port", 1143)))
    current["smtp_port"] = int(prompt("SMTP port", current.get("smtp_port", 1025)))
    source = Path(prompt("Exported Bridge public certificate (.pem)", current.get("cert_path")) or "").expanduser()
    if not source.is_file() or source.stat().st_size > 65_536:
        raise ValueError("Choose the exported public certificate file, at most 64 KiB.")
    pem = source.read_text(encoding="ascii")
    if "PRIVATE KEY" in pem or pem.count("-----BEGIN CERTIFICATE-----") != 1:
        raise ValueError("Use one public certificate only; do not supply a private key or certificate bundle.")
    der = ssl.PEM_cert_to_DER_cert(pem)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cadata=pem)
    print("Bridge certificate SHA-256: " + hashlib.sha256(der).hexdigest())
    target_cert = path.parent / "bridge-cert.pem"
    if target_cert.exists() and target_cert.read_text(encoding="ascii") != pem:
        if input("Replace the previously trusted Bridge certificate? Type replace: ").strip() != "replace":
            raise ValueError("Certificate replacement cancelled.")
    current["cert_path"] = str(target_cert)
    current["mode"] = args.mode or current.get("mode", "read-only")
    current = validate_config(current)
    install_runtime()
    private_directory(path.parent)
    fd, candidate = tempfile.mkstemp(prefix=".bridge-cert-check-", suffix=".pem", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as out:
            out.write(pem)
        print("\nEnter the Bridge-generated password twice. It is not the Proton account password.")
        prompt_and_store("proton_mail_bridge_plugin", current["username"], "Bridge password")
        verify_connection(dict(current, cert_path=candidate))
        # Keep the last trusted certificate and configuration intact on failure.
        private_write(target_cert, pem)
        private_write(path, json.dumps(current, ensure_ascii=False, indent=2) + "\n")
    finally:
        if os.path.exists(candidate):
            os.unlink(candidate)
    print("\nSetup complete. Mode: " + current["mode"])
    print("Local MCP launch: python3 " + str(ROOT / "run.py"))
    print("Reopen the plugin's local MCP connection to load the configuration.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ValueError as exc:
        # These are local schema/setup validation errors, not mail server text.
        print("Setup/check failed: " + str(exc), file=sys.stderr)
        sys.exit(1)
    except (OSError, ssl.SSLError, imaplib.IMAP4.error, subprocess.SubprocessError) as exc:
        # Avoid server-returned text, subprocess argv, and passwords in diagnostics.
        print("Setup/check failed (" + type(exc).__name__ + "). Check Bridge, STARTTLS, the exported certificate, and Keychain.", file=sys.stderr)
        sys.exit(1)
