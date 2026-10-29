#!/usr/bin/env python3
"""Run the existing stdio plugin through OpenAI's native tunnel on a logged-in Mac.

Commands: install [--tunnel-id ID] [--tunnel-client ABSOLUTE_PATH] [--replace-key],
status, stop. The internal serve command is supervised by a per-user LaunchAgent.
No tunnel is created and no account plugin is changed by this helper.
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from keychain_store import SecretInputError, prompt_and_store

ROOT = Path(__file__).resolve().parent
LABEL = "local.proton-mail-bridge.tunnel"
PROFILE = "proton-mail-bridge"
KEYCHAIN_SERVICE = "proton_mail_bridge_tunnel"
MARKER = "Managed by Proton Mail Bridge deploy_mini.py"
TUNNEL_PATTERN = re.compile(r"tunnel_(?:[a-z0-9]{4}_)?[a-z0-9]{32}\Z")


class DeploymentError(ValueError):
    """Safe diagnostics written by this helper, never subprocess output."""


def paths():
    base = Path.home() / ".config/proton-mail-bridge-plugin/tunnel"
    return {"base": base, "settings": base / "deployment.json",
            "profile": base / (PROFILE + ".yaml"), "health": base / "health-url",
            "pid": base / "runtime.pid", "log": base / "runtime.log",
            "handoff": base / "handoff.json",
            "plist": Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")}


def checked_id(value):
    if not isinstance(value, str) or not TUNNEL_PATTERN.fullmatch(value):
        raise DeploymentError("Enter the complete tunnel ID returned by OpenAI. Syntax validation does not verify ownership or workspace association.")
    return value


def require_mac():
    if sys.platform != "darwin" or os.getuid() == 0:
        raise DeploymentError("Run this as your normal logged-in macOS user, without sudo, on the Mac running Bridge.")


def private_dir(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise DeploymentError("The deployment directory must be a real directory owned by you.")
    path.chmod(0o700)


def private_file(path, *, missing=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing:
            return False
        raise DeploymentError("A deployment file is missing. Run install again.") from None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
        raise DeploymentError("Deployment files must be owner-only regular files, without symlinks or hard links.")
    return True


def atomic_write(path, content):
    if path.exists() or path.is_symlink():
        private_file(path)
    fd, name = tempfile.mkstemp(prefix=".deploy-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content if isinstance(content, bytes) else content.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def run(args, *, env=None, check=True, timeout=30):
    try:
        result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        raise DeploymentError("A local deployment command could not run. Check the installed executables and retry.") from None
    if check and result.returncode:
        raise DeploymentError("A deployment check failed. Run the documented local diagnostic command; captured output is suppressed to protect credentials.")
    return result


def executable(value):
    path = Path(value).expanduser()
    if not path.is_absolute() or any(ord(c) < 32 for c in str(path)) or not path.is_file() or not os.access(path, os.X_OK):
        raise DeploymentError("Choose an existing executable using an absolute path.")
    # Preserve a Homebrew opt symlink path across formula upgrades.
    return str(path)


def find_tunnel_client(explicit=None):
    if explicit:
        return executable(explicit)
    brew = shutil.which("brew")
    if not brew:
        raise DeploymentError("Install the official client first: brew install openai/tools/tunnel-client; then rerun install.")
    result = run([brew, "--prefix", "tunnel-client"], check=False)
    if result.returncode or not result.stdout.strip():
        raise DeploymentError("Install the official client first: brew install openai/tools/tunnel-client; then rerun install.")
    return executable(str(Path(result.stdout.strip()) / "bin/tunnel-client"))


def keychain_key(tunnel_id):
    result = run(["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
                  "-a", checked_id(tunnel_id), "-w"], check=False, timeout=15)
    key = result.stdout.rstrip("\n")
    if result.returncode or not key or any(ord(c) < 32 for c in key):
        raise DeploymentError("The tunnel runtime key is unavailable in Keychain. Unlock your login keychain or run install --replace-key locally.")
    return key


def ensure_key(tunnel_id, replace=False):
    if not replace:
        try:
            return keychain_key(tunnel_id)
        except DeploymentError:
            pass
    if not sys.stdin.isatty():
        raise DeploymentError("Enter the runtime API key in your own interactive Mac terminal. Never paste the key in chat.")
    print("Enter the complete runtime API key twice. Use a key with Tunnels Read + Use, not an admin key.")
    try:
        return prompt_and_store(
            KEYCHAIN_SERVICE, checked_id(tunnel_id), "Runtime API key",
            validator=lambda value: re.fullmatch(r"sk-[A-Za-z0-9_-]{37,8192}", value) is not None,
        )
    except SecretInputError as exc:
        raise DeploymentError(str(exc)) from None



def clean_environment():
    # Explicit allowlist: avoid inherited admin keys, shell-only overrides, or
    # unrelated MCP/control-plane settings silently replacing this profile.
    env = {name: os.environ[name] for name in ("HOME", "USER", "LOGNAME", "PATH", "TMPDIR", "LANG", "LC_ALL") if name in os.environ}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    return env


def runtime_environment(settings, key):
    env = clean_environment()
    env["CONTROL_PLANE_API_KEY"] = key
    env["PROTON_MAIL_BRIDGE_CONFIG"] = settings["bridge_config"]
    return env


def load_settings():
    path = paths()["settings"]
    if not private_file(path, missing=True):
        return None
    if path.stat().st_size > 16_384:
        raise DeploymentError("Deployment settings are unexpectedly large.")
    try:
        settings = json.loads(path.read_text())
    except (ValueError, UnicodeError):
        raise DeploymentError("Deployment settings are invalid. Run install again.") from None
    required = {"tunnel_id", "tunnel_client", "python", "package_root", "bridge_config"}
    if not isinstance(settings, dict) or set(settings) != required:
        raise DeploymentError("Deployment settings have unsupported fields.")
    checked_id(settings["tunnel_id"])
    for name in required - {"tunnel_id"}:
        if not isinstance(settings[name], str) or not Path(settings[name]).is_absolute() or any(ord(c) < 32 for c in settings[name]):
            raise DeploymentError("Deployment paths must be absolute local paths.")
    return settings


def service_state():
    result = run(["/bin/launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], check=False)
    if result.returncode and not (result.returncode == 113 and "could not find service" in (result.stdout + result.stderr).lower()):
        raise DeploymentError("Could not verify the prior launchd service state. No new runtime was started.")
    pid_match = re.search(r"^\s*pid = (\d+)\s*$", result.stdout, re.MULTILINE)
    return {"loaded": result.returncode == 0, "pid": int(pid_match.group(1)) if pid_match else None}


def process_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def stop_service():
    state = service_state()
    if state["loaded"]:
        run(["/bin/launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"])
    deadline = time.monotonic() + 10
    while service_state()["loaded"]:
        if time.monotonic() >= deadline:
            raise DeploymentError("The prior launchd service did not stop. Do not start another runtime for this tunnel.")
        time.sleep(0.1)
    if state["pid"]:
        deadline = time.monotonic() + 10
        while process_alive(state["pid"]):
            if time.monotonic() >= deadline:
                raise DeploymentError("The old runtime is still exiting. Wait before rerunning install; overlapping stdio runtimes are not supported.")
            time.sleep(0.1)


def plist_data(settings):
    p = paths()
    return {"Label": LABEL, "Comment": MARKER,
            "ProgramArguments": [settings["python"], str(Path(settings["package_root"]) / "deploy_mini.py"), "serve"],
            "WorkingDirectory": settings["package_root"],
            "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 30,
            "Umask": 0o077, "StandardOutPath": str(p["log"]), "StandardErrorPath": str(p["log"])}


def health_status(settings, state):
    result = {"process_running": bool(state["pid"]) and process_alive(state["pid"]),
              "healthy": False, "ready": False, "control_plane_poll_seen": False}
    p = paths()
    if not state["loaded"] or not state["pid"] or not p["health"].exists():
        return result
    try:
        private_file(p["health"])
        if p["health"].stat().st_size > 1024:
            return result
        url = urlsplit(p["health"].read_text().strip())
        if url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port or url.username or url.password or url.query or url.fragment or url.path not in ("", "/"):
            return result
        report = run([settings["tunnel_client"], "health", "--url-file", str(p["health"]),
                      "--pid", str(state["pid"]), "--require-control-plane-poll", "--json"], check=False, timeout=15)
        data = json.loads(report.stdout)
        # Output only selected booleans, not native logs or response bodies.
        process = data.get("process", {})
        result["process_running"] = process.get("running") is True and process.get("pid") == state["pid"]
        if not result["process_running"]:
            return result
        result["healthy"] = data.get("healthz", {}).get("ok") is True
        result["control_plane_poll_seen"] = data.get("control_plane_poll", {}).get("ok") is True
        result["ready"] = (report.returncode == 0 and data.get("result") == "ok"
                           and result["healthy"] and data.get("readyz", {}).get("ok") is True
                           and result["control_plane_poll_seen"])
    except (DeploymentError, OSError, ValueError, TypeError, AttributeError):
        pass
    return result


def status_data():
    settings = load_settings()
    if not settings:
        return {"installed": False, "remote_mail_verified": False}
    state = service_state()
    return {"installed": True, "tunnel_id": settings["tunnel_id"],
            "id_validation": "syntax_only", "service_loaded": state["loaded"],
            **health_status(settings, state),
            "remote_mail_verified": False,
            "next_check": "In the linked ChatGPT plugin, call proton_connection_status with test_connection=true."}


def install(args):
    require_mac()
    sys.path.insert(0, str(ROOT / "server"))
    from configuration import config_path, load_config
    from run import runtime_path
    from setup import verify_connection
    cfg = load_config()
    if cfg is None:
        raise DeploymentError("Complete python3 setup.py on this Mac before installing its tunnel service.")
    python = executable(str(runtime_path() / "bin/python"))
    run([python, "-c", "import mcp, pydantic"], env=clean_environment())
    verify_connection(cfg)
    old = load_settings()
    tunnel_id = args.tunnel_id or (old or {}).get("tunnel_id")
    if not tunnel_id:
        if not sys.stdin.isatty():
            raise DeploymentError("Pass --tunnel-id with the actual ID created in OpenAI Platform.")
        tunnel_id = input("Actual OpenAI tunnel ID (not an API key): ").strip()
    tunnel_id = checked_id(tunnel_id)
    binary = find_tunnel_client(args.tunnel_client)
    run([binary, "--version"])
    settings = {"tunnel_id": tunnel_id, "tunnel_client": binary, "python": python,
                "package_root": str(ROOT), "bridge_config": str(config_path())}
    key = ensure_key(tunnel_id, args.replace_key)
    if not key.startswith("sk-") or len(key) < 40:
        raise DeploymentError("The saved runtime key does not look like an OpenAI API key. Rerun install with --replace-key and enter the complete secret key at the secure prompt.")
    env = runtime_environment(settings, key)
    p = paths()
    private_dir(p["base"])
    # Build and validate the new profile before stopping a working service.
    with tempfile.TemporaryDirectory(prefix=".profile-", dir=p["base"]) as staging:
        run([binary, "init", "--sample", "sample_mcp_stdio_local", "--profile", PROFILE,
             "--profile-dir", staging, "--tunnel-id", tunnel_id,
             "--control-plane-api-key-ref", "env:CONTROL_PLANE_API_KEY",
             "--mcp-command", shlex.join([python, str(ROOT / "run.py")]),
             "--health-listen-addr", "127.0.0.1:0"], env=env)
        candidate = Path(staging) / (PROFILE + ".yaml")
        run([binary, "doctor", "--profile-file", str(candidate), "--json"], env=env)
        profile = candidate.read_bytes()
        if key.encode("utf-8") in profile:
            raise DeploymentError("The generated profile unexpectedly contains a credential. It was not installed.")
    if p["plist"].exists() or p["plist"].is_symlink():
        private_file(p["plist"])
        if plistlib.loads(p["plist"].read_bytes()).get("Comment") != MARKER:
            raise DeploymentError("A different service owns this LaunchAgent path; it was not changed.")
    stop_service()
    # Avoid stale health URLs after a service restart. These are our own files.
    for name in ("health", "pid"):
        if private_file(p[name], missing=True):
            p[name].unlink()
    atomic_write(p["profile"], profile)
    atomic_write(p["settings"], json.dumps(settings, indent=2) + "\n")
    if not private_file(p["log"], missing=True):
        atomic_write(p["log"], "")
    agents = p["plist"].parent
    agents.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = agents.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise DeploymentError("LaunchAgents must be your real directory and not writable by other users.")
    atomic_write(p["plist"], plistlib.dumps(plist_data(settings)))
    run(["/bin/launchctl", "enable", f"gui/{os.getuid()}/{LABEL}"])
    run(["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(p["plist"])])
    env.pop("CONTROL_PLANE_API_KEY", None)
    key = None
    result = status_data()
    atomic_write(p["handoff"], json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    print("Service loaded. Keep this package directory in place: " + str(ROOT))
    print("Readiness may still be pending. Recheck: " + shlex.join([python, str(ROOT / "deploy_mini.py"), "status"]))
    print("Nonsecret handoff: " + str(p["handoff"]))
    print("Share only the tunnel ID and status. Account association and a successful ChatGPT mail-status call are still required.")
    return 0


def serve():
    require_mac()
    settings = load_settings()
    if not settings:
        raise DeploymentError("No deployment settings. Run install first.")
    executable(settings["python"])
    binary = executable(settings["tunnel_client"])
    if not (Path(settings["package_root"]) / "run.py").is_file():
        raise DeploymentError("The installed package moved. Put it back or rerun install from its permanent location.")
    p = paths()
    private_file(p["profile"])
    env = runtime_environment(settings, keychain_key(settings["tunnel_id"]))
    os.umask(0o077)
    os.chdir(settings["package_root"])
    argv = [binary, "run", "--profile-file", str(p["profile"]),
            "--health.url-file", str(p["health"]),
            "--pid.file", str(p["pid"]), "--log.level=warn"]
    os.execve(binary, argv, env)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    installer = sub.add_parser("install", help="Configure and load this user's persistent tunnel service.")
    installer.add_argument("--tunnel-id", help="Actual OpenAI tunnel ID; this helper does not create one.")
    installer.add_argument("--tunnel-client", help="Explicit stable absolute path to the official executable.")
    installer.add_argument("--replace-key", action="store_true", help="Open the local macOS Keychain password prompt.")
    sub.add_parser("status", help="Print nonsecret service and tunnel health; does not access mail or Keychain.")
    sub.add_parser("stop", help="Disable and unload this service; preserve its configuration and Keychain item.")
    sub.add_parser("serve", help=argparse.SUPPRESS)
    args = parser.parse_args()
    require_mac()
    if args.command == "install":
        return install(args)
    if args.command == "serve":
        return serve()
    if args.command == "stop":
        run(["/bin/launchctl", "disable", f"gui/{os.getuid()}/{LABEL}"])
        stop_service()
        print("Service unloaded and disabled for future logins. Configuration and Keychain item retained; install enables it again.")
        return 0
    print(json.dumps(status_data(), indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except DeploymentError as exc:
        print("Deployment: " + str(exc), file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print("Deployment failed (" + type(exc).__name__ + "). Inspect the local prerequisites; credentials and subprocess output are not printed.", file=sys.stderr)
        sys.exit(1)
