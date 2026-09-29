"""Run every release gate and print a summary (docs/RELEASING.md).

Usage: .venv/bin/python scripts/release_check.py [--skip-fresh-install]
"""
from __future__ import annotations

import filecmp
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from _tooling import ROOT, SERVER, WEB, node, tool_path

PY = str(SERVER / ".venv" / "bin" / "python")
ENV = dict(os.environ, PATH=tool_path(), TMPDIR=tempfile.mkdtemp(prefix="opa-release-"))


def sh(cmd, cwd=SERVER) -> tuple[bool, str]:
    r = subprocess.run(cmd, cwd=cwd, env=ENV, capture_output=True, text=True, shell=isinstance(cmd, str))
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def locks() -> tuple[bool, str]:
    ok, out = sh(["uv", "lock", "--locked"])
    if not ok:
        return False, "uv.lock is out of date with pyproject.toml: " + out[-300:]
    tmp = Path(tempfile.mkdtemp())
    ok, out = sh([PY, "scripts/export_locks.py", str(tmp)])
    if not ok:
        return False, out[-300:]
    for name in ("server.txt", "browser.txt"):
        if not filecmp.cmp(tmp / name, ROOT / "deploy" / "locks" / name, shallow=False):
            return False, f"deploy/locks/{name} does not match uv.lock; run scripts/export_locks.py"
    return True, "uv.lock current; deploy/locks match it"


def api_types() -> tuple[bool, str]:
    tmp = Path(tempfile.mkdtemp())
    ok, out = sh([PY, "scripts/export_openapi.py", str(tmp / "openapi.json")])
    if not ok or not filecmp.cmp(tmp / "openapi.json", WEB / "openapi.json", shallow=False):
        return False, "web/openapi.json is stale: run scripts/export_openapi.py ../web/openapi.json and npm run gen:api"
    ok, out = sh([node(), "node_modules/openapi-typescript/bin/cli.js", str(tmp / "openapi.json"), "-o", str(tmp / "gen.ts")], cwd=WEB)
    if not ok or not filecmp.cmp(tmp / "gen.ts", WEB / "src" / "api-types.gen.ts", shallow=False):
        return False, "web/src/api-types.gen.ts is stale: run npm run gen:api"
    return True, "OpenAPI document and generated types are current"


GATES = [
    ("Lockfiles current", locks),
    ("Lint (ruff)", lambda: sh([str(SERVER / ".venv" / "bin" / "ruff"), "check", "opa", "tests", "scripts", "--select", "E,F,W", "--ignore", "E501", "-q"])),
    ("Tests (pytest, incl. release-gate tests)", lambda: sh([PY, "-m", "pytest", "-q", "-p", "no:cacheprovider"])),
    ("Migrations (no drift, reversible head)", lambda: sh([PY, "scripts/check_migrations.py"])),
    ("No secrets in the repository", lambda: sh([PY, "scripts/check_secrets.py"])),
    ("API types generated and current", api_types),
    ("Web type-check (incl. API contract) and build", lambda: sh(f'"{node()}" node_modules/typescript/bin/tsc -b && "{node()}" node_modules/vite/bin/vite.js build', cwd=WEB)),
    ("Browser smoke with axe-core (A17a)", lambda: sh([PY, "scripts/ui_smoke.py"])),
    ("Wheel installs and migrates outside the source tree", lambda: sh([PY, "scripts/check_wheel.py"])),
    ("Fresh install from the README", lambda: sh([PY, "scripts/fresh_install_check.py"])),
]


def main() -> int:
    skip = {"Fresh install from the README"} if "--skip-fresh-install" in sys.argv else set()
    results = []
    for name, gate in GATES:
        if name in skip:
            results.append((name, None, 0.0, "skipped"))
            continue
        t = time.monotonic()
        ok, out = gate()
        results.append((name, ok, time.monotonic() - t, (out.strip().splitlines() or [""])[-1][:140]))
        print(f"{'PASS' if ok else 'FAIL'}  {name}  ({results[-1][2]:.0f}s)  {results[-1][3]}", flush=True)
        if not ok:
            print(out[-3000:])
    failed = [r for r in results if r[1] is False]
    print(f"\n{len(results) - len(failed) - len(skip)} passed, {len(failed)} failed, {len(skip)} skipped")
    print("Manual gates not covered here: A17b VoiceOver pass; Docker Compose build and run; live Google and Claude checks.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
