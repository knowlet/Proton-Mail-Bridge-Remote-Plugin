#!/usr/bin/env python3
"""Launch only the hash-pinned environment prepared by setup.py."""
import hashlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def runtime_path():
    digest = hashlib.sha256((ROOT / "server/requirements.txt").read_bytes()).hexdigest()[:16]
    return Path.home() / ".local/share/proton-mail-bridge-plugin" / ("venv-" + digest)


def main():
    python = runtime_path() / "bin/python"
    if not python.is_file():
        print("Proton Mail Bridge plugin needs local setup first. Run on the Mac with Bridge:\n"
              + "  python3 " + str(ROOT / "setup.py"), file=sys.stderr)
        return 2
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1")
    env.pop("PYTHONPATH", None)
    # A native tunnel launches the stdio child with its own environment. The
    # mail process needs Bridge credentials only, never tunnel/admin/API keys.
    for name in ("CONTROL_PLANE_API_KEY", "OPENAI_ADMIN_KEY", "OPENAI_API_KEY"):
        env.pop(name, None)
    os.execve(str(python), [str(python), str(ROOT / "server/bridge_plugin.py")], env)


if __name__ == "__main__":
    sys.exit(main())
