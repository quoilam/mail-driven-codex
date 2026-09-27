"""Install or remove the per-user macOS LaunchAgent for mail-oo."""

import argparse
import os
import plistlib
import shutil
import subprocess
import time
from pathlib import Path


LABEL = "com.mail-oo.agent"
PROJECT = Path(__file__).resolve().parent.parent
TARGET = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
DOMAIN = f"gui/{os.getuid()}"


def install():
    python = PROJECT / ".local" / "venv" / "bin" / "python"
    config = PROJECT / ".env"
    if not config.exists():
        config = PROJECT / ".local" / "config.json"
    if not python.is_file() or not config.is_file():
        raise SystemExit("先安装 .local/venv，并创建私有 .env 或 .local/config.json")
    subprocess.run([str(python), "-m", "mailoo", "check", "--config", str(config)],
                   cwd=PROJECT, check=True)
    codex = shutil.which("codex")
    if not codex:
        raise SystemExit("未找到 codex CLI")
    local = PROJECT / ".local"
    local.mkdir(mode=0o700, exist_ok=True)
    logs = local / "logs"
    logs.mkdir(mode=0o700, exist_ok=True)
    search_path = ":".join(dict.fromkeys([str(Path(codex).parent), str(python.parent),
                                           "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin",
                                           "/bin", "/usr/sbin", "/sbin"]))
    payload = {
        "Label": LABEL,
        "ProgramArguments": [str(python), "-m", "mailoo", "run", "--config", str(config)],
        "WorkingDirectory": str(PROJECT),
        "RunAtLoad": True,
        "KeepAlive": True,
        "EnvironmentVariables": {"PATH": search_path},
        "StandardOutPath": str(logs / "launch.out.log"),
        "StandardErrorPath": str(logs / "launch.err.log"),
    }
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_bytes(plistlib.dumps(payload))
    TARGET.chmod(0o600)
    subprocess.run(["launchctl", "bootout", f"{DOMAIN}/{LABEL}"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    # launchd can take a moment to release a service after bootout.
    for attempt in range(10):
        result = subprocess.run(["launchctl", "bootstrap", DOMAIN, str(TARGET)],
                                capture_output=True, text=True)
        if result.returncode == 0:
            break
        if attempt == 9:
            raise SystemExit(result.stderr.strip() or "launchctl bootstrap failed")
        time.sleep(0.5)
    print(f"installed and started {LABEL}")


def uninstall():
    subprocess.run(["launchctl", "bootout", f"{DOMAIN}/{LABEL}"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    TARGET.unlink(missing_ok=True)
    print(f"removed {LABEL}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("install", "uninstall", "status"))
    args = parser.parse_args()
    if args.command == "install":
        install()
    elif args.command == "uninstall":
        uninstall()
    else:
        subprocess.run(["launchctl", "print", f"{DOMAIN}/{LABEL}"], check=True)


if __name__ == "__main__":
    main()
