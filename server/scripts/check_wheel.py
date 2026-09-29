"""Release gate: the package installs like the Docker images install it, and works outside the source tree.

Builds the wheel, installs the hash-pinned server lock plus the wheel (no dependency resolution) into a fresh
virtualenv, imports every module, runs `opa version`, and runs `opa migrate` from an unrelated directory.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from _tooling import ROOT, SERVER, tool_path


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        raise SystemExit(f"FAILED: {' '.join(map(str, cmd))}\n{(r.stdout + r.stderr)[-1500:]}")
    return r.stdout


def main() -> int:
    env = dict(os.environ, PATH=tool_path())
    work = Path(tempfile.mkdtemp(prefix="opa-wheelcheck-"))
    try:
        run(["uv", "build", "--wheel", "--out-dir", str(work / "dist")], cwd=SERVER, env=env)
        venv = work / "venv"
        run(["uv", "venv", "-q", "--seed", "-p", "3.12", str(venv)], env=env)
        pip = venv / "bin" / "pip"
        run([str(pip), "install", "-q", "--require-hashes", "-r", str(ROOT / "deploy" / "locks" / "server.txt")])
        run([str(pip), "install", "-q", "--no-deps", *map(str, (work / "dist").glob("*.whl"))])
        run([str(pip), "install", "-q", "pgserver"])  # test-only: a throwaway database for the migrate check
        code = ("import importlib, pkgutil, opa\n"
                "for m in pkgutil.walk_packages(opa.__path__, 'opa.'):\n"
                "    if '.migrations.' not in m.name: importlib.import_module(m.name)\n"
                "print('imports ok')")
        print(run([str(venv / "bin" / "python"), "-c", code], cwd="/").strip())
        print(run([str(venv / "bin" / "opa"), "version"], cwd="/").strip())
        mig = ("import os, subprocess, pgserver, tempfile\n"
               "d = tempfile.mkdtemp(); db = pgserver.get_server(d)\n"
               "env = dict(os.environ, OPA_DATABASE_URL=db.get_uri().replace('postgresql://', 'postgresql+asyncpg://'))\n"
               f"r = subprocess.run(['{venv}/bin/opa', 'migrate'], env=env, cwd='/', capture_output=True, text=True)\n"
               "db.cleanup(); print(r.stdout.strip() or r.stderr.strip()[-400:]); raise SystemExit(r.returncode)")
        print(run([str(venv / "bin" / "python"), "-c", mig], cwd="/").strip())
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
