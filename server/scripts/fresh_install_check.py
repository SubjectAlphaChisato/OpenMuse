"""Release gate: a fresh installation works from the README (spec section 11).

Copies the files git would ship (tracked plus untracked-but-not-ignored, so no venv, node_modules, builds, or
secrets) into an empty directory, runs the README's install block verbatim, checks the database and owner
setup commands, then runs the full browser smoke from that copy with its own installed binaries.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from _tooling import ROOT, tool_path


def main() -> int:
    keep = "--keep" in sys.argv
    dest = Path(tempfile.mkdtemp(prefix="opa-fresh-"))
    env = dict(os.environ, PATH=tool_path(), TMPDIR=str(dest / "tmp"))
    (dest / "tmp").mkdir()
    try:
        files = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
        for name in files:
            src = ROOT / name
            if src.is_file():
                (dest / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest / name)
        readme = (dest / "README.md").read_text()
        m = re.search(r"<!-- fresh-install:begin -->\s*```sh\n(.*?)```\s*<!-- fresh-install:end -->", readme, re.S)
        if not m:
            print("FAIL README has no fresh-install block")
            return 1
        print(f"copied {len(files)} files to {dest}; running the README install block:\n{m.group(1)}")
        r = subprocess.run(["bash", "-euo", "pipefail", "-c", m.group(1)], cwd=dest, env=env, capture_output=True, text=True)
        if r.returncode != 0:
            print("FAIL install block:", (r.stdout + r.stderr)[-2000:])
            return 1
        opa = dest / "server" / ".venv" / "bin" / "opa"
        py = dest / "server" / ".venv" / "bin" / "python"
        print(subprocess.run([str(opa), "version"], capture_output=True, text=True, check=True).stdout.strip())
        setup = f"""
import os, subprocess, pgserver
db = pgserver.get_server({str(dest / 'tmp' / 'pg')!r})
env = dict(os.environ, OPA_DATABASE_URL=db.get_uri().replace('postgresql://', 'postgresql+asyncpg://'),
           OPA_APP_DB_PASSWORD='app-pw', OPA_BROKER_DB_PASSWORD='broker-pw')
for args, stdin in ((['migrate'], None), (['bootstrap-owner', 'owner@example.com'], 'fresh-install-pass\\nfresh-install-pass\\n'), (['db-roles'], None)):
    r = subprocess.run([{str(opa)!r}, *args], env=env, input=stdin, capture_output=True, text=True)
    print('opa', args[0], '->', r.returncode, (r.stdout.strip().splitlines() or [''])[-1])
    if r.returncode: raise SystemExit(1)
db.cleanup()
"""
        r = subprocess.run([str(py), "-c", setup], env=env, capture_output=True, text=True)
        print(r.stdout.strip())
        if r.returncode != 0:
            print("FAIL setup:", r.stderr[-1500:])
            return 1
        r = subprocess.run([str(py), str(dest / "server" / "scripts" / "ui_smoke.py")], cwd=dest / "server", env=env, capture_output=True, text=True)
        print("\n".join(line for line in r.stdout.splitlines() if line.startswith(("PASS", "FAIL", "UI smoke"))))
        if r.returncode != 0:
            print("FAIL browser smoke from the fresh copy:", (r.stdout + r.stderr)[-2000:])
            return 1
        print("fresh install ok")
        return 0
    finally:
        if keep:
            print("kept", dest)
        else:
            shutil.rmtree(dest, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
