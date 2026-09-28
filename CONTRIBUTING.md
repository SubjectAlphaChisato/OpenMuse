# Contributing

- Read `docs/open-muse-implementation-requirements.md` and `docs/tech-design.md` first. Changes should cite the requirement ID they satisfy.
- Keep the app runnable at every milestone. Do not substitute a mock for a missing milestone or present a fixture as a live integration.
- Run `cd server && .venv/bin/python -m pytest` and `.venv/bin/ruff check opa tests scripts` before sending changes. UI changes should also pass `scripts/ui_smoke.py`.
- Schema changes go through Alembic: `OPA_DATABASE_URL=... .venv/bin/alembic revision --autogenerate -m "..."`, then review the generated file.
- Update `IMPLEMENTATION_STATUS.md` when a requirement or acceptance test changes status.
- Never commit secrets. `.env` is ignored; `.env.example` documents every variable without values.
