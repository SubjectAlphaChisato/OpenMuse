# Open Personal Agent

A self-hostable personal agent: persistent chat, durable background tasks, and (in later milestones) Gmail and Google Calendar connectors with enforced human approval for every external write.

**Status: milestones M1 (Foundation) and M2 (Broker and approvals) of six.** See [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md). Connectors run against a built-in demo account only; real Google sign-in, artifacts, memory, scheduling, and the browser are not built yet.

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

## Tests

```sh
cd server && .venv/bin/python -m pytest          # unit + acceptance, embedded Postgres, no network
.venv/bin/python scripts/ui_smoke.py             # real processes + headless Chromium (build web first)
```

## Not yet documented

Google API enablement and OAuth registration, backup, restore, and teardown arrive with milestones M3 and M6.

## License

Apache-2.0. See [LICENSE](LICENSE).
