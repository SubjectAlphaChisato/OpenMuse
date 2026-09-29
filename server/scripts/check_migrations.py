"""Release gate: models match migrations, and the newest migration downgrades and re-applies cleanly."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

import pgserver

from opa.db.migrate import alembic_config, current_head


def main() -> int:
    from alembic import command
    from alembic.script import ScriptDirectory

    d = tempfile.mkdtemp(prefix="opa-migcheck-")
    db = pgserver.get_server(d)
    url = db.get_uri().replace("postgresql://", "postgresql+asyncpg://")
    try:
        cfg = alembic_config(url)
        command.upgrade(cfg, "head")
        env = dict(os.environ, OPA_DATABASE_URL=url)
        r = subprocess.run([sys.executable, "-m", "alembic", "check"], cwd=os.path.dirname(os.path.dirname(__file__)), env=env, capture_output=True, text=True)
        if r.returncode != 0:
            print("model/migration drift:", (r.stdout + r.stderr).strip()[-600:])
            return 1
        head = current_head()
        previous = ScriptDirectory.from_config(cfg).get_revision(head).down_revision
        command.downgrade(cfg, previous)
        command.upgrade(cfg, "head")
        print(f"migrations ok: no drift; head {head} downgrades to {previous} and re-applies")
        return 0
    finally:
        db.cleanup()
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
