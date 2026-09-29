# Open Personal Agent

A self-hostable personal agent: persistent chat, durable background tasks, and (in later milestones) Gmail and Google Calendar connectors with enforced human approval for every external write.

**Status: all six milestones implemented; release gates pending.** See [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md). Chat, background tasks, approvals, Gmail and Google Calendar (live verification pending), files, memory, schedules, calendar-change triggers, a restricted public-web browser, export, delete-all, and backup and restore work; a built-in demo account works without any credentials. Still open before a release: the accessibility gates (A17), a fresh Docker Compose install, and live Google and Claude checks.

- Requirements: [docs/open-muse-implementation-requirements.md](docs/open-muse-implementation-requirements.md)
- Technical design: [docs/tech-design.md](docs/tech-design.md)

## Run locally without Docker

Requires [uv](https://docs.astral.sh/uv/) and Node 20+. PostgreSQL 16 is needed; for development the test suite starts an embedded one automatically.

```sh
cd server
uv venv -p 3.12 && uv pip install -e ".[dev]"
export OPA_DATABASE_URL=postgresql+asyncpg://USER:PASS@localhost:5432/opa
.venv/bin/opa migrate
.venv/bin/opa bootstrap-owner you@example.com     # password is read from a hidden prompt

cd ../web && npm install && npm run build
cd ../server
export OPA_INTERNAL_TOKEN=$(openssl rand -hex 32)  # shared by api, worker, and broker
OPA_WEB_DIST_DIR=../web/dist .venv/bin/opa api    # http://127.0.0.1:8080
.venv/bin/opa broker                               # second terminal, port 8081
.venv/bin/opa worker                               # third terminal
.venv/bin/opa scheduler                            # fourth terminal: schedules and calendar-change triggers
.venv/bin/opa proxy --policy public-web --listen 127.0.0.1:3129   # fifth: the browser's only route out
OPA_WEB_PROXY_URL=http://127.0.0.1:3129 .venv/bin/opa browser    # sixth: isolated Chromium (port 8082)
```

Give the api and worker `OPA_BROWSER_URL=http://127.0.0.1:8082` to enable browsing. Chromium comes from `.venv/bin/playwright install chromium`.

```sh
```

In the app, open Connections and connect the demo account to try approvals without any external service. Accounts start read only; switch Gmail or Calendar to “Read and propose changes” to let the assistant propose writes, which always wait for your approval.

For production-like isolation, create separate database roles after migrating (`opa db-roles`) and point the broker at the `opa_broker` role with `OPA_BROKER_DATABASE_URL`. Re-run `opa db-roles` after every migration.

For UI development, run `npm run dev` in `web/` instead; Vite proxies `/api` to port 8080.

## Run with Docker Compose

```sh
cp .env.example .env        # set the three database passwords and OPA_INTERNAL_TOKEN
server/.venv/bin/opa generate-kek   # paste the output into OPA_BROKER_KEK
docker compose --env-file .env -f deploy/compose.yaml up --build -d
docker compose --env-file .env -f deploy/compose.yaml run --rm api opa bootstrap-owner you@example.com
```

The API binds to `127.0.0.1:8080` only. The worker has no internet route except an egress proxy that allows only the model host; only the broker can reach provider APIs. For remote access, put it behind an HTTPS reverse proxy and set `OPA_SECURE_COOKIES=true`.

## Model configuration

`OPA_MODE=demo` with `OPA_MODEL_PROVIDER=scripted` needs no keys and makes no network calls. For real use set `OPA_MODE=live`, `OPA_MODEL_PROVIDER=anthropic`, and `OPA_ANTHROPIC_API_KEY`. Missing configuration appears as a setup banner in the UI rather than a silent fallback.

## Connect Google (live mode)

The app only ever requests these scopes, and write scopes only when you allow changes:

| Capability | Read only | Read and propose changes adds |
|---|---|---|
| Gmail | `https://www.googleapis.com/auth/gmail.readonly` | `https://www.googleapis.com/auth/gmail.send` |
| Calendar | `https://www.googleapis.com/auth/calendar.readonly` | `https://www.googleapis.com/auth/calendar.events` |

Plus `openid` and `email` to identify the account.

1. In Google Cloud Console, create a project and enable the **Gmail API** and **Google Calendar API**.
2. Configure the **OAuth consent screen**. For personal use choose *External*, keep the app in *Testing*, and add your own Google address as a test user. If you have a Google Workspace domain, *Internal* avoids the limits below.
3. Create an **OAuth client ID** of type *Web application* with this exact authorized redirect URI: `http://127.0.0.1:8080/api/oauth/google/callback` (or your HTTPS origin plus `/api/oauth/google/callback`, also set as `OPA_GOOGLE_REDIRECT_URI`).
4. Put the client ID in `OPA_GOOGLE_CLIENT_ID` for the api and broker, and the secret in `OPA_GOOGLE_CLIENT_SECRET` for the broker only. Set `OPA_MODE=live`.
5. In the app, open Connections and choose **Connect with Google**.

**Limits you should know about.** Gmail read and send are *restricted* scopes. While an External app is in Testing, Google expires its refresh tokens 7 days after sign-in; the app warns you on day 6 and shows **Reconnect**. Publishing an External app with restricted scopes requires Google's verification and a security assessment, which is outside this project. Calendar scopes are *sensitive*, not restricted.

**Opt-in live check.** After connecting an account, `server/scripts/live_smoke_google.py` runs read-only calls against real Google (instructions in the file). It is never run by the test suite; writes need extra flags and a throwaway calendar.

## Tests

```sh
cd server && .venv/bin/python -m pytest          # unit + acceptance, embedded Postgres, no network
.venv/bin/python scripts/ui_smoke.py             # real processes + headless Chromium (build web first)
```

## The browser

The assistant can read public web pages through an isolated Chromium. It cannot sign in, submit forms, upload, download, buy anything, or reach private or local networks: every connection, redirect, image, script, and websocket goes through a proxy that resolves the address itself and refuses private, loopback, link-local, and cloud-metadata addresses. If a page address would carry your private data (text from your email, calendar, files, or memory), the assistant asks for your approval first and shows exactly what would be shared. Open **Browser** to watch live, **Take control** (the assistant pauses), **Resume** (cookies and site data from your turn are cleared first), or **Stop**.

## Your data

**Your data** has **Create export** (a ZIP of everything you created, with a JSON Schema for each file; no passwords, tokens, or credentials) and **Delete all data** (type your email address to confirm). Delete-all cancels running work, disconnects and revokes accounts, and removes your data and files; your sign-in stays. Backups taken earlier still contain your data until you delete them.

## Backup, restore, and teardown

Backups need the PostgreSQL 16 client tools (`pg_dump`, `pg_restore`); set `OPA_PG_BIN_DIR` if they are not on `PATH`. Run them with the database **owner** role.

```sh
# local
OPA_DATABASE_URL=postgresql+asyncpg://OWNER:PASS@localhost:5432/opa .venv/bin/opa backup /path/to/backups/2026-10-01
# Compose (writes to ./backups on the host)
docker compose --env-file .env -f deploy/compose.yaml --profile ops run --rm ops opa backup /backups/2026-10-01
```

A backup is a directory with `app.dump` (the database), `artifacts.tar.gz` (your files), and `manifest.json` (SHA-256 of each). Add `--broker` to also dump the encrypted account credentials to `broker.dump`; they are useless without `OPA_BROKER_KEK`, which you must back up separately and never store with the backup.

To restore, point the commands at an **empty** database and an **empty** artifact directory:

```sh
OPA_DATABASE_URL=postgresql+asyncpg://OWNER:PASS@localhost:5432/opa_restored OPA_ARTIFACT_DIR=/new/artifacts \
  .venv/bin/opa restore /path/to/backups/2026-10-01
.venv/bin/opa db-roles    # re-create role grants on the restored database
```

Restore verifies every file against the manifest first. Afterwards nothing resumes on its own: automations are paused (resume each in **Automations**), pending approvals are cancelled, a write that was in flight during the backup is marked "outcome unknown" for you to check, and interrupted tasks are blocked for review.

Teardown: `docker compose -f deploy/compose.yaml down` stops everything and keeps your data volumes; `down -v` also deletes the database, files, and exports permanently. Take a backup first if you might need them.

## License

Apache-2.0. See [LICENSE](LICENSE).
