"""Write the hash-pinned requirement files the Docker images install, from uv.lock.

Usage: .venv/bin/python scripts/export_locks.py [OUT_DIR]   (default: ../deploy/locks)
Headers are omitted so the output depends only on uv.lock, never on where it was written.
"""
import subprocess
import sys
from pathlib import Path

from _tooling import ROOT, SERVER, tool_path

LOCKS = {"server.txt": [], "browser.txt": ["--extra", "browser"]}


def export(out_dir: Path) -> None:
    import os

    out_dir.mkdir(parents=True, exist_ok=True)
    for name, extra in LOCKS.items():
        subprocess.run(["uv", "export", "-q", "--frozen", "--no-dev", "--no-emit-project", "--no-header", *extra,
                        "--format", "requirements-txt", "-o", str(out_dir / name)],
                       cwd=SERVER, env=dict(os.environ, PATH=tool_path()), check=True)


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "deploy" / "locks"
    export(target)
    print(f"wrote {', '.join(LOCKS)} to {target}")
