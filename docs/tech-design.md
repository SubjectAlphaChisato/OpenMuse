# Open Personal Agent — Technical Design (v0.1)

Status: Draft 1 • 2026-09-27 • Implements `open-muse-implementation-requirements.md` v0.1.1

This document turns the requirements into concrete components, schemas, algorithms, and file layout. Requirement IDs (AUTH-01, TASK-01, …) are cited where a design choice satisfies one. Where the spec leaves a choice open, this document makes it and says why.

---

## 1. Stack and repository layout

| Layer | Choice | Reason |
|---|---|---|
| Web UI | React 18, TypeScript, Vite, TanStack Query, React Router | Spec default; generated API types from OpenAPI |
| API / worker / broker / browser service | Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2 (async), Alembic | Spec default; one language for all services and the SDK |
| Database | PostgreSQL 16 | Spec; also used as job queue, event outbox, and full-text index |
| Model | Anthropic SDK, `claude-opus-5` default, scripted adapter for tests | One real provider adapter plus deterministic adapter (DEV-01) |
| Browser | Playwright (Python) driving Chromium, behind an egress proxy | Live view and takeover via CDP; proxy enforces WEB-01 |
| Auth | argon2-cffi hashes, server-side sessions, Authlib for Google OAuth | AUTH-01, CON-01 |
| Files | Local volume `/data/artifacts`, content-addressed by SHA-256 | ART-01 |
| Renderers | markdown-it-py → HTML → WeasyPrint for PDF; stdlib `csv`; pypdf for extraction | Trusted, fixed renderers (ART-01) |
| Packaging | Docker Compose, one image per service, `.env.example` | Spec deliverables |

```
/
├── web/                     # React app
├── server/
│   ├── opa/                 # Python package
│   │   ├── api/             # FastAPI routers, auth, SSE
│   │   ├── worker/          # job loop, agent loop, model adapter
│   │   ├── broker/          # tool registry, adapters, credential store, dispatch
│   │   ├── browser/         # Playwright service + egress proxy client
│   │   ├── artifacts/       # storage, renderers, extraction
│   │   ├── scheduler/       # automations, calendar trigger
│   │   ├── sdk/             # Pydantic connector SDK (spec §6)
│   │   ├── db/              # models, migrations (alembic), roles
│   │   ├── proxy.py         # egress proxy (asyncio CONNECT, allowlist policy)
│   │   └── core/            # settings, clock, ids, hashing, errors
│   └── tests/
├── deploy/compose.yaml
├── docs/
└── IMPLEMENTATION_STATUS.md
```

One Python package, several entry points (`opa api`, `opa worker`, `opa broker`, `opa browser`, `opa scheduler`). Sharing a package keeps Pydantic models and DB models in one place; the trust boundary is enforced by process, network, and database role, not by package layout.

---

## 2. Process topology and trust boundaries

```
 browser (user) ──HTTPS──▶ api ──────────────┐
                              │ SQL (app role)│
                              ▼               │
                          postgres ◀──────────┤ SQL (app role)
                              ▲               │
             SQL (broker role)│               ▼
                           broker ◀──HTTP──▶ worker ──HTTP──▶ egress-proxy(model allowlist) ──▶ model API
                              │
                              ├──HTTPS──▶ Google APIs
                              │
                           browser-svc ──▶ egress-proxy(public-web policy) ──▶ public web
                              ▲
                     api (live-view WS relay, commands)
```

Compose networks:

| Network | Members | Purpose |
|---|---|---|
| `frontend` | api | Host-published port 127.0.0.1:8080 only |
| `app` | api, worker, scheduler, broker, browser-svc, postgres | Internal RPC and SQL |
| `model-egress` | worker, egress-proxy | Worker's only external route (spec §3 item 2) |
| `web-egress` | browser-svc, egress-proxy | Browser's only external route |
| `provider-egress` | broker | Direct internet for Google APIs |

Worker and browser-svc have no default gateway on `app`; their only route to the internet is the proxy on their egress network. The proxy runs two listeners with different policies (section 9). Test T-NET-01 asserts a worker `GET https://example.com` fails at the proxy with 403.

Database roles (spec §3 item 6):

- `opa_app`: owns all tables except `broker.*`. Used by api, worker, scheduler.
- `opa_broker`: owns schema `broker` (credentials, provider idempotency records, dispatch claims) and has read access to `public.connections`, `public.actions`, `public.approvals`, `public.tasks`, `public.jobs` for pre-dispatch verification, plus update on `actions` state columns.
- Token encryption key `BROKER_KEK` is an env var only on the broker container. Envelope encryption: per-connection DEK wrapped by KEK, AES-256-GCM via `cryptography`.

Internal RPC (worker → broker, api → browser-svc) is plain HTTP on the `app` network with a shared bearer `INTERNAL_TOKEN`. It is never reachable from the host or from the browser's Chromium.

---

## 3. Data model

All tables have `id` (ULID stored as `text`), `owner_id`, `created_at`, `updated_at` (UTC). Only the notable columns and invariants are listed.

```sql
owners(id, email, password_hash, time_zone, preferences jsonb, assistant_name, response_style)
sessions(id, owner_id, token_hash, expires_at, csrf_secret)

conversations(id, owner_id, title, is_default bool, deleted_at)
messages(id, conversation_id, seq bigint, role, content text, task_id, artifact_ids text[],
         client_request_id, UNIQUE(conversation_id, seq), UNIQUE(owner_id, client_request_id))

tasks(id, owner_id, conversation_id, intent text, state, plan jsonb, checkpoint jsonb,
      version int, limits jsonb, usage jsonb, blocker_code, blocker_detail, retry_of,
      injected_memory_ids text[], active_ms bigint, finished_at)
jobs(id, task_id UNIQUE, ready_at, attempt int, lease_owner, lease_token, lease_expires_at,
     heartbeat_at, version int)

actions(id, task_id, connection_id, tool_name, tool_version, canonical_args jsonb,
        args_hash text, attachment_hashes jsonb, state, provider_idempotency_key,
        provider_refs text[], result jsonb, error jsonb, dispatch_started_at, dispatch_finished_at,
        lease_token_at_dispatch)
approvals(id, action_id, owner_id, args_hash, state, expires_at, decided_at,
          UNIQUE(action_id) WHERE state IN ('pending','approved'))

connections(id, owner_id, provider, provider_account_id, status, mode, granted_scopes text[],
            last_health_at, last_error, credential_ref)
broker.credentials(id = credential_ref, connection_id, dek_wrapped bytea, refresh_token_enc bytea,
                   access_token_enc bytea, access_expires_at, refresh_lock_at)
broker.dispatch_claims(action_id PK, claimed_at, worker_lease_token)

memories(id, owner_id, text, kind, scope, source_ref jsonb, confirmed bool, tags text[],
         version int, tsv tsvector GENERATED, deleted_at)

artifacts(id, owner_id, task_id, name, mime, current_version int, deleted_at)
artifact_versions(artifact_id, version, storage_key, size, sha256, source_refs jsonb,
                  created_by_task_id, PRIMARY KEY(artifact_id, version))

automations(id, owner_id, kind 'once'|'daily'|'weekly'|'calendar_trigger', prompt, connection_ids,
            time_zone, local_time, weekdays int[], run_at, enabled, next_run_at, last_run_at,
            trigger_state jsonb, version int, paused_reason)
occurrences(automation_id, scheduled_for timestamptz, task_id, late bool, coalesced_count int,
            PRIMARY KEY(automation_id, scheduled_for))

provider_events(id, connection_id, resource_id, version_key, payload_ref, observed_at,
                UNIQUE(connection_id, resource_id, version_key))

events(id bigserial, owner_id, type, task_id, action_id, payload jsonb, created_at)   -- outbox / SSE replay
notifications(id, owner_id, event_id, type, title, body, read_at)
audit_log(id, owner_id, actor, type, subject_id, sanitized jsonb)

browser_sessions(id, owner_id, task_id, state, control 'agent'|'user', urls_visited jsonb,
                 started_at, ended_at)
```

Invariants enforced in the database, not only in code:

- One active lease per job: `jobs.task_id UNIQUE`, and lease updates always include `WHERE lease_token = $expected`.
- One effective approval per action: the partial unique index above.
- One occurrence per schedule slot: the composite primary key.
- Provider event dedupe: unique `(connection_id, resource_id, version_key)`.
- Memory full-text: `tsv` generated column with a GIN index (MEM-01 retrieval).

---

## 4. Request path and authentication (AUTH-01)

- `opa bootstrap-owner` prompts with `getpass`, hashes with argon2id, writes the single owner row. Re-running refuses if an owner exists unless `--reset-password` is given.
- Login issues a random 256-bit session token; the DB stores its SHA-256. Cookie: `HttpOnly; Secure (if HTTPS); SameSite=Lax; Path=/`.
- CSRF: double-submit with a per-session secret. Every mutating request carries `X-CSRF-Token`, compared in constant time. SSE and WebSocket upgrades validate `Origin`.
- Throttle: login attempts limited to 5 per 15 minutes per IP and per email, stored in a `login_attempts` table.
- Every router dependency resolves `current_owner` and every query filters by `owner_id`. The ownership check for object routes is a single helper `get_owned(Model, id, owner)` that raises 404 on miss, so another owner's objects are indistinguishable from missing ones (A14).
- `/healthz` returns `{"ok": true}` only.

---

## 5. Task execution

### 5.1 Job leasing (TASK-01)

Worker loop, every 1 s when idle:

```sql
WITH cand AS (
  SELECT j.id FROM jobs j JOIN tasks t ON t.id = j.task_id
  WHERE j.ready_at <= now()
    AND (j.lease_expires_at IS NULL OR j.lease_expires_at < now())
    AND t.state IN ('queued')
  ORDER BY j.ready_at
  FOR UPDATE SKIP LOCKED LIMIT 1
)
UPDATE jobs SET lease_owner=$worker, lease_token=$new_token,
       lease_expires_at = now() + interval '60 seconds', heartbeat_at = now(),
       attempt = attempt + 1, version = version + 1
FROM cand WHERE jobs.id = cand.id
RETURNING jobs.*;
```

Then in the same transaction `UPDATE tasks SET state='running', version=version+1 WHERE id=$task AND state='queued'`.

Heartbeat every 10 s: `UPDATE jobs SET lease_expires_at = now()+60s, heartbeat_at=now() WHERE id=$job AND lease_token=$token`. Zero rows updated means the lease was lost; the worker sets an in-process `lease_lost` flag, stops issuing broker calls, and abandons the run without writing a checkpoint.

Every checkpoint write and every broker dispatch request carries `lease_token`. Checkpoints use `UPDATE tasks ... WHERE id=$task AND version=$expected AND EXISTS (SELECT 1 FROM jobs WHERE task_id=$task AND lease_token=$token AND lease_expires_at > now())`. The broker performs the same existence check before recording `dispatching` (fencing).

### 5.2 Recovery

A recovery sweep runs in the worker every 30 s:

1. Find tasks in `running` whose job lease expired.
2. For each, check `actions` in state `dispatching` or `unknown` for that task. If any exist, set the task to `blocked` with `blocker_code = 'unknown_external_outcome'` and emit a notification. Do not requeue.
3. Otherwise set task `queued`, clear the lease. The next claim resumes from `checkpoint`.

### 5.3 Agent loop (one run)

```
load task, checkpoint, confirmed memory (§8), conversation tail, tool registry filtered by connection mode
loop:
  enforce budgets (30 model calls, 50 dispatches, 10 min active, per spec TASK-02)
  resp = model.generate(messages, tools, limits)
  if resp.stop_reason == 'refusal': block task with blocker 'model_refused', persist explanation, stop
  for each tool_use in resp (executed concurrently for reads, sequentially for writes):
      validate input against tool.input_schema (jsonschema); invalid → tool_result is_error
      if tool.effect == 'read':  broker.dispatch(read) with retry policy
      if tool.effect == 'write': create action (§6); task → waiting_approval; persist checkpoint; return
      if tool is a browser/artifact/memory tool: call the corresponding service
  append all tool_results in one user message; checkpoint (messages, usage, step)
  if resp.stop_reason == 'end_turn': finalize (§5.4)
```

Checkpoint contents: the message list (tool inputs and results, no thinking blocks), plan, current step index, usage counters, and elapsed active time. Thinking blocks are neither stored nor displayed (TASK-01). Because the history is append-only, the same checkpoint can be replayed on the same model after a restart.

Active-time accounting: `active_ms` accumulates only while the task is `running`. Waiting states do not count.

### 5.4 Success criteria

`succeeded` requires: every action created by the task is in `succeeded`, `denied`, or `cancelled` (none in `unknown`/`dispatching`); every artifact referenced in the plan's required outputs exists. Otherwise the task ends `failed` with partial outputs retained, or `blocked` if any action is `unknown`.

### 5.5 Stop, resume, retry (TASK-02)

- Cancel: `UPDATE tasks SET state='cancelled' WHERE id=$id AND state NOT IN terminal`; publish `task.cancel` event. Broker checks `tasks.state` before every dispatch, so the next dispatch is refused. Browser service gets a `stop` command. Pending approvals for the task become `cancelled`. An action already in `dispatching` is left to finish and its outcome is recorded.
- Resume: allowed from `blocked` or `waiting_user`; sets `queued`. For `unknown_external_outcome` the owner must first mark the action reconciled (§6.4).
- Retry: creates a new task with `retry_of`, copies only artifacts and action results in `succeeded`.

---

## 6. External actions and approvals (APP-01, spec §5)

### 6.1 Action creation

Canonical args: JSON with sorted keys, no whitespace, NFC-normalized strings, attachments replaced by `{artifact_id, version, sha256}`. `args_hash = sha256(tool_name + '@' + tool_version + '\n' + canonical_json)`. Action ID is a ULID generated once and stored before the approval card is shown.

Provider idempotency key is generated at creation: for `calendar.create_event` a base32hex string (Google event ID rules) stored in `provider_idempotency_key`. Gmail has no equivalent; the field is null.

### 6.2 Approval

`POST /api/approvals/{id}/decision` body: `{decision: 'allow_once'|'deny', expected_args_hash}`. The API:

1. Loads the approval with `FOR UPDATE`; verifies owner, `state='pending'`, `expires_at > now()`, and `expected_args_hash == approvals.args_hash == actions.args_hash`.
2. Re-checks `connections.status='connected'` and `mode='read_write'`, and `tasks.state='waiting_approval'`.
3. On allow: approval `approved`, action `approved`, task `queued` (job `ready_at=now()`), all in one transaction. On deny: approval `denied`, action `denied`, task `blocked` with `blocker_code='approval_denied'`.
4. A repeated decision on a non-pending approval returns 200 with the current state (idempotent), not a new decision.

Any change to the proposal (recipient, body, artifact version) creates a new action and a new approval; the old approval is set to `superseded` by the worker. Expiry is enforced both by the API check and by a scheduler sweep that marks pending approvals `expired` and blocks their tasks.

### 6.3 Dispatch (broker)

```
BEGIN
  SELECT action FOR UPDATE                       -- must be 'approved'
  verify: approval state 'approved' and hash match; connection connected + read_write;
          task state 'queued' or 'running'; job lease_token matches request and not expired
  INSERT broker.dispatch_claims(action_id, ...)   -- PK conflict ⇒ duplicate; return existing state
  UPDATE actions SET state='dispatching', dispatch_started_at=now(), lease_token_at_dispatch=$token
COMMIT
result = adapter.execute(...)   -- network
BEGIN
  UPDATE actions SET state = result.status, provider_refs, result/error, dispatch_finished_at
COMMIT
```

If the process dies between the two transactions, the action stays `dispatching`; the recovery sweep (§5.2) converts `dispatching` older than the per-tool timeout (120 s) into `unknown`, never into a retry.

### 6.4 Reconciliation

- Calendar: `GET events/{persisted_id}`. Found ⇒ `succeeded` with provider ref. 404 ⇒ safe to mark `failed` and allow a new action with the same idempotency key. Anything else stays `unknown`.
- Gmail: search sent mail for `rfc822msgid:` of the locally generated Message-ID header within 10 minutes of dispatch. Found ⇒ `succeeded`. Not found ⇒ remain `unknown`; the UI shows a "check your Sent folder" card with a link and a "mark as not sent / mark as sent" choice. Marking as not sent unblocks a new action with a new approval and a duplicate-risk warning. Never auto-resend.

---

## 7. Connector broker and adapters (CON-01, GMAIL-01, CAL-01)

### 7.1 Registry

Tools are Pydantic `ToolDefinition` instances registered per provider module. The worker receives only the definitions for connections whose mode permits the effect (`read_only` ⇒ reads only; `off` ⇒ none). The model never sees a generic HTTP tool.

### 7.2 Credentials and OAuth

- `POST /api/connectors/google/authorize` creates an `oauth_states` row bound to the session, with PKCE verifier, requested scopes, and a 10-minute expiry, then redirects to Google with `access_type=offline`, `prompt=consent`, `include_granted_scopes=true`.
- Callback verifies state, exchanges the code (Authlib), and hands tokens to the broker over internal RPC. The broker encrypts and stores them; the api stores only `credential_ref`.
- Scope policy: `calendar.readonly` and `gmail.readonly` at first connect; `calendar.events` and `gmail.send` requested incrementally when the owner switches a capability to `read_write`. Full URIs are in `server/opa/broker/google/scopes.py`.
- Refresh: single-flight per connection using `broker.credentials.refresh_lock_at` with a 30-second lock; a 401 from Google triggers one refresh then one retry. `invalid_grant` ⇒ connection `reauth_required`, dependent automations `paused_reason='reauth_required'`, pending approvals `invalidated`, notification emitted.
- Disconnect: mark `disconnected` first, invalidate approvals and pause automations in the same transaction, then best-effort `POST https://oauth2.googleapis.com/revoke`. Revocation failure is recorded, access is not restored.
- Testing-mode expiry: the daily health job warns when `connected_at + 6 days` has passed and the OAuth client is flagged `testing` in settings.

### 7.3 Adapters

Gmail (google-api-python-client):

| Tool | Implementation notes |
|---|---|
| `gmail.search_messages` | `users.messages.list(q, maxResults≤100, pageToken)`; then batch `messages.get(format=metadata)` for From/To/Subject/Date/snippet |
| `gmail.get_message` | `format=full`; walk MIME parts; prefer `text/plain`, else HTML→text via `html2text` with links kept; cap 50 KB |
| `gmail.get_thread` | `threads.get`, bounded to 20 messages, newest last |
| `gmail.send_message` | Build RFC 2822 with a locally generated `Message-ID`; base64url raw; `users.messages.send`; attachments from artifact versions by hash |

Security filter (GMAIL-01): a deterministic classifier drops messages whose subject or body matches OTP, password-reset, or magic-link patterns from `broker/google/sensitive_mail.py`; dropped IDs are reported as `filtered_count`, never their content. Message bodies are wrapped in the tool result as `{"untrusted": true, "text": ...}` and the system prompt states that content inside tool results is data.

Calendar:

| Tool | Implementation notes |
|---|---|
| `calendar.list_calendars` | `calendarList.list` |
| `calendar.list_events` | `events.list(timeMin, timeMax, singleEvents=true, orderBy=startTime)`, paginated |
| `calendar.get_event` | `events.get`; returns `etag` |
| `calendar.create_event` | `events.insert` with client `id` from the action; reject attendees; validate `end > start`; refuse past events unless the action carries `confirm_past=true` |
| `calendar.update_event` | Re-`get`; compare `etag` to the one captured at proposal time; mismatch ⇒ `conflict`; refuse if `recurringEventId` present; `If-Match` header on `patch` |

### 7.4 Fixture adapter

`fixtures/` implements the same tool set against in-memory JSON scenarios loaded from `server/tests/fixtures/*.json`. It supports scripted failures (`raise_after_dispatch`, `timeout_after_send`) for A04 and A05. In `demo` mode the API marks every connection `provider='fixture'` and the UI shows the DEMO badge; in `live` mode the fixture provider is not registered at all (DEV-01).

---

## 8. Memory (MEM-01)

Retrieval, run once per task run start and recorded in `tasks.injected_memory_ids`:

1. `SELECT * FROM memories WHERE owner_id=$o AND confirmed AND deleted_at IS NULL AND scope='global' AND tags && '{style,identity}'`.
2. `SELECT *, ts_rank(tsv, q) FROM memories, plainto_tsquery('english', $intent || ' ' || $last_user_msg) q WHERE confirmed AND (scope='global' OR scope=$conversation_id) AND NOT (tags && '{style,identity}') AND tsv @@ q ORDER BY rank DESC, updated_at DESC`.
3. Append in order until 8,000 characters.

Writes: the `memory.remember` tool creates `kind='user_statement', confirmed=true` only when the current user message contains an explicit remember request (checked by the worker against the message, not by the model). `kind='inference'` items are created unconfirmed and surface as a "suggested memory" card. The Markdown editor renders records as a list with hidden IDs; `PUT /api/memory` diffs against the stored version and returns 409 on mismatch.

---

## 9. Browser service (WEB-01)

### 9.1 Egress proxy

A small asyncio HTTP proxy (`proxy/`) with two listeners:

- `:3128` model policy: allow `CONNECT` only to the configured model host on 443.
- `:3129` public-web policy: allow `CONNECT` and absolute-URI `GET`/`HEAD` on ports 80/443. The proxy resolves the hostname itself, rejects any resolved address in loopback, RFC 1918, link-local, CGNAT, multicast, or unique-local ranges, and connects to that exact resolved address. Non-HTTP schemes are refused. Blocked attempts are logged with the browser session ID.

Chromium flags: `--proxy-server=http://egress-proxy:3129 --proxy-bypass-list=<-loopback> --host-resolver-rules="MAP * ~NOTFOUND"`. The first sends everything, including localhost, through the proxy; the last makes Chromium unable to resolve names itself, so DNS rebinding cannot bypass the proxy check. Redirects, subresources, and websockets all go through `CONNECT` and are checked per connection (A12).

The browser container has no volumes and no env secrets, runs as a non-root user with a seccomp profile, and only reaches `app` (for the service's own API) and `web-egress`.

### 9.2 Session control

Browser service API (internal): `create`, `command(navigate|snapshot|click|scroll|open_tab|close_tab)`, `takeover`, `resume`, `stop`. `snapshot` returns the accessibility tree flattened to a bounded text form (max 30 KB) with numbered link refs; `click` accepts a ref, never a selector or JS.

Data-sharing check: before `navigate`, the worker asks the api whether the URL contains any string longer than 12 characters that also appears in the task's loaded memory, connector results, or artifact text; if so the navigation becomes a write-like action requiring approval (WEB-01 data-sharing rule).

### 9.3 Live view

`GET /api/browser-sessions/{id}/stream` is a WebSocket on the api, which relays to the browser service. Downstream frames: `{"t":"frame","jpeg":base64,"w":..,"h":..}` from CDP `Page.startScreencast` (quality 60, max 1280 wide, ≤10 fps). Upstream during `control='user'` only: `{"t":"mouse"|"key"|"wheel", ...}` mapped to `Input.dispatchMouseEvent` / `Input.dispatchKeyEvent`. While `control='user'`, agent commands return `paused` and the worker parks the task in `waiting_user`. Fallback: `GET .../screenshot` returns the latest JPEG.

---

## 10. Scheduler (AUTO-01, AUTO-02)

One `opa scheduler` process, tick every 15 s, all times computed with `zoneinfo`.

Next-run computation for `daily`/`weekly`: take the next calendar date in the automation's zone, build the local wall time with `fold=0`; if the resulting instant does not exist (spring gap), advance minute by minute until it does. Because `fold=0` picks the first occurrence in a fall-back hour, repeated hours run once.

Enqueue: `INSERT INTO occurrences(automation_id, scheduled_for) ... ON CONFLICT DO NOTHING RETURNING *`; only a returned row creates a task. This makes duplicate ticks and two scheduler instances safe.

Catch-up after downtime: on start, for each enabled automation with `next_run_at < now()`:
- `once`: enqueue with `late=true`.
- recurring: compute all missed slots, insert one occurrence for the most recent slot with `coalesced_count = missed - 1`, then advance `next_run_at`.

No overlap: if the previous occurrence's task is not terminal, record the new slot with `task_id = NULL` and `coalesced_count += 1`; when the running task finishes, the scheduler creates one task for the pending slot.

Calendar trigger: every 5 minutes per trigger automation, `events.list(syncToken)` looping over pages; on HTTP 410 clear the token and do a full window sync comparing against retained `(event_id, etag)` in `trigger_state`. A change is relevant when the event's start is within 48 hours and its `(id, etag)` differs from the retained one and is not in the automation's own recorded action refs. Relevant changes are collected for 60 s, then one task is enqueued per automation with the change set. Initial sync stores the baseline and enqueues nothing.

---

## 11. Artifacts (ART-01)

Storage key: `/data/artifacts/{owner_id}/{sha256[:2]}/{sha256}`. Filenames are metadata only; the on-disk name is the hash, so traversal is impossible by construction and duplicates share bytes. Write to a temp file in the same directory, fsync, `os.replace`, then insert the version row; a cleanup job deletes files with no version row older than one hour.

Upload pipeline: size check (20 MiB), `python-magic` sniff must agree with the declared type, filename sanitized to `[A-Za-z0-9._-]`, then extraction in a subprocess with a 30 s timeout and a 2 MB text cap. PDFs with under 50 characters of extracted text per page on average return `unsupported_format`.

Typed tools: `artifact.create_markdown`, `artifact.edit_markdown(expected_version, patch)`, `artifact.create_csv(rows)`, `artifact.render_pdf(from_artifact, version)`. CSV writer prefixes cells starting with `= + - @` with a single quote and the tool result says so. Markdown preview uses `markdown-it-py` with `html=False` plus `bleach` on the output.

---

## 12. Model adapter (DEV-01)

Interface: `generate(messages, tools, limits) -> GenerateResult(text, tool_calls, stop_reason, usage)`.

Anthropic adapter, from the Claude API reference:

- Default model `claude-opus-5`; operator can set `MODEL_ID`. `max_tokens` 16000, non-streaming (tool inputs here are small).
- Omit `thinking` so adaptive thinking applies; `output_config.effort` from settings, default `high`. Never send `budget_tokens`.
- Every tool definition has `strict: true`, `additionalProperties: false`, and a `required` list, generated from the Pydantic input model.
- `tool_choice` is left on `auto`; the system prompt names the expected tool when one is required. Never use forced tool choice.
- Return all `tool_result` blocks for one assistant turn in a single user message, failed ones with `is_error: true`.
- Prompt-cache breakpoints after the system prompt and the tool list; volatile content (time, task) goes after.
- Handle `stop_reason == "refusal"` as a task blocker with the `stop_details` explanation. Enable server-side fallbacks with `betas=["server-side-fallback-2026-07-01"]` and `fallbacks="default"` so refusal categories route automatically; the operator can disable this in settings.
- Usage: record `input_tokens`, `output_tokens`, cache read/write tokens. Cost is shown only when `MODEL_PRICE_IN`/`MODEL_PRICE_OUT` are configured, else "unknown".

Scripted adapter: loads a YAML script keyed by step number with fixed tool calls and text, used by the acceptance suite. Setup state: if `MODEL_PROVIDER` is unset or the key is missing in `live` mode, `/api/me` reports `setup_required: ['model']` and the UI shows the setup screen; no task can be created.

---

## 13. Events, SSE, notifications (UX-01)

`events` is the outbox. Every state change that the UI must see inserts an event row in the same transaction as the change. `GET /api/events?after={id}` streams rows with `id > after` for the owner, then `LISTEN opa_events` for new ones, sending `id:` on every frame so `EventSource` reconnects with `Last-Event-ID`. Notifications are derived rows created by the same transaction for types `task.succeeded|failed|blocked`, `approval.requested`, `connection.reauth_required`. Because the client always resumes from the last ID it processed, reconnects deliver each event once (A19).

---

## 14. Export, delete, backup (DATA-01)

- Export: a background task writes `export-{ts}.zip` under `/data/exports/{owner}/` containing `schema_version.json`, one JSON file per entity, and `artifacts/{id}/{version}-{name}`. Connections are exported with `credential_ref` removed. Download via an owner-checked route.
- Delete all: requires the body `{"confirm": "<owner email>"}`; cancels tasks, disconnects providers (revocation attempted), deletes rows in dependency order, removes artifact files, and writes one audit row.
- Backup: `opa backup` runs `pg_dump --format=custom` for `public` and tars `/data/artifacts`; `opa backup --broker` separately dumps `broker.*` and requires `BROKER_KEK` to be backed up out of band. `opa restore` loads both, sets every automation `enabled=false, paused_reason='restored'`, marks pending approvals `invalidated`, and marks `dispatching` actions `unknown`.

---

## 15. Testing strategy

- Unit: pure functions (canonicalization, DST next-run, tsv ranking, proxy address classification).
- Integration: pytest with a real Postgres (testcontainers), the fixture connector, the scripted model, and an injectable `Clock` used by the scheduler, lease logic, and approval expiry. The 20 acceptance tests from spec §11 map one-to-one to `server/tests/acceptance/test_a01_persistence.py` … `test_a20_demo_live.py`.
- Two-worker tests (A03) start two worker instances against one DB and assert one dispatch claim.
- Browser: Playwright tests against a fixture site served in the compose `test` profile, plus a "private" target on the `app` network that must be unreachable (A12).
- UI: Playwright end-to-end for the approval keyboard flow and axe-core scans of the six required screens (A17a). A17b is a manual checklist in `docs/accessibility-checklist.md`.
- Live smoke (opt-in, `LIVE_SMOKE=1`): read-only Gmail search and calendar list against a test account; the send and create-event smokes require `LIVE_SMOKE_WRITES=1` and target addresses/calendars named in env.

---

## 16. Milestone mapping

| Milestone | Sections above | Exit evidence |
|---|---|---|
| M1 | 1, 2 (compose skeleton), 3, 4, 5, 12 (scripted adapter), 13 | A01, A14, A15, A19 |
| M2 | 6, 7.1, 7.4, 2 (roles, proxy) | A02, A03, A04, A05, A18, T-NET-01 |
| M3 | 7.2, 7.3 | A06, A07, live smoke documented |
| M4 | 8, 11 | A10, A11 |
| M5 | 10 | A08, A09 |
| M6 | 9, 14, packaging | A12, A13, A16, A17, A20 |

---

## 17. Open decisions

1. **Screencast versus VNC.** Design uses CDP screencast for simplicity. If input fidelity during takeover proves poor (drag, IME), switch to noVNC in the browser container; the WebSocket route stays the same.
2. **WeasyPrint system dependencies** add ~150 MB to the image. Alternative is reportlab with a smaller feature set. Decide at M4. Decided in §19: reportlab.
3. **Data-sharing detection heuristic** in 9.2 is intentionally crude. Accept false positives in v0.1 and tune with fixtures.
4. **Model choice.** `claude-opus-5` is the default for capability. Operators wanting lower cost can set `claude-sonnet-5`; no automatic downgrade.

---

## 18. Decisions recorded during M2

1. **Tool names on the wire.** Claude tool names cannot contain dots, so `gmail.send_message` is sent to the model as `gmail__send_message`. Internal names, the action ledger, and audit records keep the dotted form.
2. **One connection, per-capability modes.** A Google account is one connection with separate modes for `gmail` and `calendar`, because one OAuth grant covers both. This is where provider scopes and application permissions are kept apart (CON-01).
3. **Every broker tool takes a `connection_id`.** It is an enum of the owner's eligible connections in the tool schema. The broker re-derives the owner from the task, so a model-supplied ID for another owner's connection is rejected.
4. **The worker writes the proposal; the broker validates and dispatches.** The broker's `prepare` returns the canonical payload, hash, and preview without creating anything. The worker then inserts the action, the approval, the checkpoint, and the `waiting_approval` transition in one transaction, so a crash cannot leave an orphan approval. The broker recomputes the hash from the stored payload before dispatch, so a tampered row cannot be sent.
5. **Previews are a pure function of the canonical payload** (`opa/broker/ops.py`), so the approval card cannot drift from what is dispatched. Calendar updates store the `before` values and the expected etag inside the payload.
6. **Denial blocks the task.** Following the state table in the spec, a denied or expired approval moves the task to `blocked`; resuming hands the model a tool error saying the action was denied or expired.
7. **One proposal per model turn.** If a turn contains several writes, the first is proposed and the rest receive a tool error asking the model to propose them after the first is decided.
8. **Fixture state belongs to the broker.** The demo account's mailbox and calendar live in `broker.fixture_state` and are created by the broker on first use, so the application role never writes to the broker schema.
9. **Column-level grants for the broker role.** The broker reads only `owners(id, preferences)`, `tasks(id, owner_id, state)`, and the lease columns of `jobs`, plus connections, actions, and approvals. Insert-returning needs `id` and `created_at` on events, notifications, and audit rows; no content columns are readable.

---

## 19. Decisions recorded during M4

1. **PDF renderer: reportlab, not WeasyPrint** (closes open decision 2). WeasyPrint needs Pango, cairo, and GDK-PixBuf from the OS, which add about 150 MB to the image and cannot be installed from pip alone. reportlab installs from wheels, and with `invariant=1` it produces byte-identical output for the same input, so a re-render does not change a hash. It also never parses HTML or loads URLs, so a rendered brief cannot make the server fetch anything. The PDF is built from markdown-it tokens (headings, paragraphs, bold and italic, inline code, lists, quotes, code blocks, rules), and every text run is XML-escaped before reportlab sees it. Trade-offs: tables and images are not rendered (image alt text is), and the built-in fonts cover Latin-1 only, so other scripts render incorrectly. If that matters, bundle a TTF font such as DejaVu Sans. That is a font change, not a new renderer.
2. **No python-magic.** It needs the libmagic system library. v0.1 accepts only four formats, and their bytes are easy to tell apart, so `opa/artifacts/files.py` uses a fixed sniffer instead: a `%PDF-` header means PDF; known binary signatures, NUL bytes, or invalid UTF-8 mean binary; anything else is text. The file's extension, the declared Content-Type (when it is not generic), and the sniffed class must all agree. Otherwise the upload fails with `type_mismatch`.
3. **nh3 instead of bleach** for sanitizing preview HTML. bleach has been deprecated since 2023, and nh3 wraps the maintained `ammonia` sanitizer. Markdown is first rendered with `html=False`, so raw HTML becomes text. The output then goes through an allowlist: no images, and links only to http, https, or mailto, with `rel="noopener noreferrer nofollow"`.
4. **Upload transport.** `POST /api/artifacts?filename=…` takes the raw file as the request body. The size cap is enforced while streaming (a 413 comes before 20 MiB is buffered), and no multipart parser is needed. Scanned PDFs, type mismatches, and extraction timeouts are rejected before any bytes are stored.
5. **Extraction sandbox.** PDF text is extracted in a child process (`python -m opa.artifacts.extract_worker`). The child runs with a minimal environment, an RLIMIT_CPU of timeout + 1 s, no core dumps, and a wall-clock kill after `OPA_EXTRACTION_TIMEOUT_SECONDS` (30 s). Output is capped at `OPA_EXTRACTION_MAX_CHARS` (2 M characters). The child has no database connection. Under 50 characters of text per page on average means `unsupported_format` (OCR needed). Encrypted PDFs are also `unsupported_format`. The child is not network-sandboxed. It runs inside the api container, which can reach the network through `frontend`. pypdf makes no network requests, but nothing at the OS level stops it; seccomp or a network namespace would close that gap.
6. **Memory retrieval ORs query terms.** `plainto_tsquery` ANDs every word, so a long request would match almost nothing. The query is rewritten with `|` between its terms, then ranked with `ts_rank`, with ties broken by `updated_at` and then ID. Items are appended in order, and the first one that does not fit under the cap ends the list. That keeps the result a prefix of the ranking.
7. **The `memory.remember` gate** (`opa/memory/gate.py`) is enforced by the worker, not the model. The owner's latest plain-text message must contain an explicit request ("remember", "keep in mind", "don't forget", "from now on", …). The text to save must also be grounded in that message: at least 60 % of its content words must appear there, and every email address, URL, or domain in it must appear there verbatim. Tool results never count as the owner's message. Without an explicit request the tool fails and points the model to `memory.suggest`, which creates only an unconfirmed `inference` that is never injected until the owner accepts it.
8. **Forgetting is a hard delete.** Deleting the row also removes its generated `tsvector` and GIN entry. The audit log keeps only the ID and kind, never the text. The editor tells the owner that source conversations and files remain until they are deleted separately.
9. **Memory editor concurrency.** The Markdown view carries a collection version: a hash of every confirmed item's `(id, version)`. `PUT /api/memory` locks the owner row, compares versions (409 `version_conflict` on mismatch), then diffs by the hidden `<!-- m:ID -->` markers. A line without a marker is a new owner statement, a changed line bumps that item's version, and a missing line forgets the item. Pending suggestions are not part of the document and cannot be removed by it.
10. **Attachment binding (APP-01, A02).** `gmail.send_message` takes `attachments: [{artifact_id, version}]`. At `prepare`, the broker resolves each reference to `{artifact_id, version, name, mime, size, sha256}` and requires the version to be the current one; the resolved list is part of the canonical payload and therefore of the approval hash. The key is omitted when there are no attachments, so hashes for emails without attachments are unchanged from M2. At dispatch, the broker re-reads the artifact row, requires that version to still be current, and re-hashes the stored bytes. Any difference cancels the action and invalidates the approval. Editing or deleting an attached file also cancels its open proposals right away and requeues a task that was waiting on one, so the model proposes again with the new version (spec section 12, step 6). Attachments are enabled only for the fixture provider until the Gmail adapter builds MIME attachments; that is a follow-up when M3 merges. For other providers `prepare` refuses them instead of silently dropping them.
11. **Broker access to files.** The broker role reads `artifacts(id, owner_id, name, mime, current_version, deleted_at)` and `artifact_versions(artifact_id, version, storage_key, size, sha256)`, never `extracted_text`. Compose mounts the artifact volume into the broker read-only.
12. **Artifact and memory tools run in the worker.** They write only to the owner's own workspace, so they need no approval and do not go through the broker. Each call is one transaction fenced on the task lease. Known gap: a crash after a tool commits but before the checkpoint can repeat the call on resume, which may add a duplicate version or memory. It has no external effect.
13. **`render_pdf` keeps a stable PDF identity.** Re-rendering the same Markdown file adds a version to the PDF that was rendered from it, rather than creating a new file.
14. **CSV escaping.** Cells starting with `= + - @`, tab, or carriage return get a leading `'`, and the tool result and the version note state how many cells changed. Plain signed numbers such as `-5` or `+1.5e3` are left alone because spreadsheets read them as numbers, not formulas. Uploaded CSVs are stored exactly as uploaded; escaping applies to CSVs the assistant creates.
15. **Deleting a file** keeps a tombstone `artifacts` row for audit, deletes its version rows, and invalidates approvals that attach it. `opa artifacts-gc` deletes stored files that no version references once they are older than one hour. It is not scheduled automatically yet; run it from cron or the M5 scheduler.
16. **Test databases.** `tests/conftest.py` puts the pgserver data directory under `tempfile.gettempdir()`. Two worktrees running pytest at the same time share that server and deadlock on `TRUNCATE`. Give each concurrent run its own `TMPDIR`.
