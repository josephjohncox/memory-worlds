#!/usr/bin/env python3
"""Launch guided setup through the checkout's locked mise toolchain."""

import os
import shutil
import subprocess
import sys
from pathlib import Path


def main():
    executable = shutil.which("mise")
    if executable is None:
        print(
            "Install mise 2026.10.17 or newer: https://mise.jdx.dev/installing-mise.html",
            file=sys.stderr,
        )
        return 1

    try:
        mise = str(Path(executable).resolve())
        root = Path(__file__).resolve().parents[1]
        environment = dict(os.environ, MEMORY_WORLDS_SETUP_CWD=str(Path.cwd()))
        # mise enforces its normal trust check. Never trust source automatically.
        installed = subprocess.run(
            [mise, "install", "--locked", "python", "uv"],
            cwd=root,
            env=environment,
            check=False,
        )
        if installed.returncode:
            return installed.returncode if installed.returncode > 0 else 128 - installed.returncode
        os.chdir(root)
        os.execve(mise, [mise, "run", "setup", "--", *sys.argv[1:]], environment)
    except KeyboardInterrupt:
        return 130
    except OSError:
        print("Setup bootstrap could not launch the locked mise toolchain", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
