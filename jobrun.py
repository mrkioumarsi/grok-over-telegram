#!/usr/bin/env python3
"""Run one Grok answer outside the Telegram bot process."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path


def main() -> None:
    folder = Path(sys.argv[1]).resolve()
    spec = json.loads((folder / "argv.json").read_text(encoding="utf-8"))
    argv = [str(part) for part in spec["argv"]]
    env = {str(key): str(value) for key, value in dict(spec.get("env") or {}).items()}
    env.pop("BOT_TOKEN", None)
    env.pop("ADMIN_ID", None)
    stdout = os.open(str(folder / "stdout"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    stderr = os.open(str(folder / "stderr"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.dup2(stdout, 1)
    os.dup2(stderr, 2)
    os.close(stdout)
    os.close(stderr)
    os.chdir(spec.get("cwd") or "/")
    proc = subprocess.Popen(argv, env=env, start_new_session=True)

    def stop(signum, _frame) -> None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            proc.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    code = proc.wait()
    exit_path = folder / "exit"
    exit_path.write_text(f"{code}\n", encoding="utf-8")
    os.chmod(exit_path, 0o600)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
