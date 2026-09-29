# Releasing

A release is ready when every automated gate passes and the manual gates below are recorded in `IMPLEMENTATION_STATUS.md`.

## Automated gates

```sh
cd server
.venv/bin/python scripts/release_check.py        # add --skip-fresh-install for a quicker run
```

| Gate | What it proves |
|---|---|
| Lockfiles current | `uv.lock` matches `pyproject.toml`, and `deploy/locks/*.txt` (what the Docker images install, with hashes) match `uv.lock` |
| Lint | ruff finds nothing |
| Tests | the full deterministic acceptance suite, plus `tests/test_release_gates.py`: every API route rejects anonymous callers, every state change needs the CSRF token, internal services need their token, and only the owner's decision endpoint can approve an action (so no approval bypass and no automatic replay of an unknown write) |
| Migrations | models match migrations, and the newest migration downgrades and re-applies |
| No secrets | no credential formats in any file git would ship; `.env` and `.mcp.json` are ignored and untracked |
| API types | `web/openapi.json` and `web/src/api-types.gen.ts` match the running API |
| Web | `tsc -b` (including the UI-to-API contract in `src/api-contract.check.ts`) and the production build |
| Browser smoke and A17a | real processes and headless Chromium through every screen, with an axe-core scan of each; serious or critical findings fail |
| Wheel | the built package installs from the lock like the images do, and migrates from outside the source tree |
| Fresh install | an empty copy of the repository follows the README's install block verbatim, sets up the database and owner, and passes the browser smoke |

## Manual gates

1. **A17b screen reader.** With VoiceOver (macOS) or NVDA, complete an approval end to end: open Approvals, read the whole card, allow it, and hear the status announcement. Record the date, tool, and result.
2. **Docker Compose.** On a machine with Docker, follow the README's Compose section from a clean checkout, sign in, connect the demo account, and approve an email. Then run `docker compose ... --profile ops run --rm ops opa backup /backups/test` and restore it into a fresh stack.
3. **Live providers**, when credentials exist: `scripts/live_smoke_google.py` (read-only by default) and one real Claude conversation in live mode. Record whether each was run; never claim live verification otherwise.

## Cutting the release

1. Set the version in `server/pyproject.toml` and `web/package.json`, update `CHANGELOG.md`, run `uv lock` and `.venv/bin/python scripts/export_locks.py`.
2. Run the automated gates and complete the manual ones.
3. Commit, then tag `vX.Y.Z` and push the tag.
