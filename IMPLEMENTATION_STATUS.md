# Implementation Status

Last updated: 2026-09-28 (M2) • Spec: `docs/open-muse-implementation-requirements.md` v0.1.1 • Design: `docs/tech-design.md`

## Milestones

| Milestone | Status |
|---|---|
| M1 Foundation | **Implemented and verified locally** (see evidence below) |
| M2 Broker and approvals | **Implemented and verified locally** (fixture provider; see evidence below) |
| M3 Google connectors | Not started |
| M4 Useful outputs | Not started |
| M5 Automation | Not started |
| M6 Browser and release | Not started |

## Requirement status

| ID | Status | Notes |
|---|---|---|
| AUTH-01 | Implemented | argon2id hashes, hidden-prompt bootstrap, server sessions, HttpOnly cookie, CSRF on mutations, login throttling (5 per 15 min per email and per IP), owner checks on every route, localhost bind. HTTPS guidance in README. |
| CHAT-01 | Implemented | Default and side conversations, ordered messages, tasks accepted while others run, `client_request_id` dedupe (409 on changed payload). The composer asks whether a message is a new task or a reply when a task is waiting for the owner. |
| TASK-01 | Implemented | Transactional lease claim with `SKIP LOCKED`, 10 s heartbeat, 60 s expiry, fencing on every checkpoint and every broker call, recovery sweep, checkpoint after each tool round, all eight display states. An action record is committed before any external write; recovery blocks instead of requeueing when an action is `dispatching` or `unknown`; a task cannot succeed while one is unresolved. |
| TASK-02 | Implemented | Cancel, explicit resume, retry as a linked new task, budgets for model calls, tool dispatches, and active runtime. Cancel invalidates open proposals and the broker refuses dispatch for tasks that are not running under a current lease. |
| DEV-01 | Implemented | `generate()` adapter interface; deterministic scripted adapter; Anthropic adapter (adaptive thinking, effort, strict tools, `auto` tool choice, refusal handling, server-side fallbacks). Demo mode refuses the real provider; `/api/me` reports setup problems. |
| UX-01 | Partial | Chat, Tasks, Approvals (exact payload, expiry, Allow once, Deny, unknown-outcome checks), Connections, and Notifications screens; durable notifications; SSE with replay by cursor and `Last-Event-ID`; semantic labels, focus styles, live-region announcements. Automations, Files, and the rest of Settings arrive with M4 and M5. |
| APP-01 | Implemented | Read-only by default; per-capability off / read only / read and propose. Every write needs a fresh Allow once or Deny for one immutable action bound to owner, connection, task, tool version, and canonical payload hash; 24 h expiry; only the authenticated decision endpoint grants. “Always ask” also gates reads. The broker re-checks everything before dispatch. Attachment hash binding waits for artifacts (M4). |
| CON-01 | Partial | Connect (demo account), permission changes with version checks, disconnect that invalidates approvals and denies new calls. Envelope-encrypted credential vault and separate broker DB role are in place. Google OAuth, refresh, revocation, and health checks are M3. |
| GMAIL-01 | Partial (fixture) | All four operations with validation, exact recipients, sensitive-mail withholding (codes, resets, sign-in links), untrusted-content marking, Message-ID reconciliation. Real Gmail adapter is M3. |
| CAL-01 | Partial (fixture) | All five operations; timed and all-day; IANA zones; end-after-start; past-event confirmation; DST-gap rejection; no attendees; recurring and attendee events cannot be edited; stale-edit detection by etag; create reconciles by persisted client ID. Real Calendar adapter is M3. |
| ART-01, MEM-01 | Not started | M4 |
| AUTO-01, AUTO-02 | Not started | M5 |
| WEB-01, DATA-01 | Not started | M6 |

## Acceptance tests

| Test | Status | Where |
|---|---|---|
| A01 Persistence | Pass (M1 scope) | `server/tests/acceptance/test_a01_persistence.py` — two tasks survive refresh; crashed worker's lease expires, second worker cannot claim early, stale worker is fenced; a different worker resumes from the checkpoint without losing outputs |
| A14 Isolation | Pass (M1 scope) | `test_a14_isolation.py` — tasks, messages, cancel, notifications, event replay |
| A15 Honest status | Pass | provider error blocks with partial outputs kept and no fabricated reply; malformed tool input is an error |
| A18 Budget/cancel | Pass (M1 scope) | loop stops at 30 model calls; cancelled task is never claimed; retry links to original |
| A19 Notification replay | Pass | replay after cursor returns each event once; SSE frames carry ids |
| A02 Approval integrity | Pass (except attachments) | `test_m2_approvals.py` — payload changed after or before approval, wrong expected hash, expiry by decision and by sweep, replayed approval. Attachment-bytes change waits for M4 artifacts. |
| A03 Duplicate execution | Pass | five concurrent dispatches of one action send once with one claim row; three concurrent decisions grant once; two workers cannot hold one task |
| A04 Ambiguous send | Pass | lost response: `unknown`, task blocked, no automatic resend, reconcile finds it by Message-ID; lost request: not found, owner marks it, still no resend |
| A05 Event recovery | Pass | lost response after insert: reconcile by persisted event ID, exactly one event; lost request: marked failed safely |
| A06 Revocation | Partial | disconnect while awaiting approval invalidates it, blocks the task, and dispatch is denied. Token revocation and automation pausing arrive with M3 and M5. |
| A18 Budget/cancel | Pass | adds: cancel while awaiting approval invalidates the action and the broker refuses dispatch |
| A07–A13, A16, A17, A20 | Not started | later milestones |

Totals on 2026-09-28: 67 pytest cases pass (embedded PostgreSQL 16, no network). Ruff clean.

## M2 boundary verification

- **Database roles:** `opa db-roles` creates `opa_app` and `opa_broker`. Tests confirm the app role cannot read the `broker` schema, and the broker role cannot read password hashes, task intents or checkpoints, message content, sessions, or event payloads. A complete propose, approve, and send flow passes with the broker connected as `opa_broker`.
- **Egress proxy policy:** the worker's proxy tunnels only allowlisted host:port pairs, refuses plain HTTP forwarding, and refuses allowlisted names that resolve to private addresses. Address classification covers loopback, RFC 1918, link-local and metadata, CGNAT, IPv6 unique-local, and IPv4-mapped IPv6.
- **Mutation check:** removing the broker's payload-hash comparison makes the tamper test fail, so it is not passing vacuously. Removing the broker's "task must be running" check does not fail any test, because cancellation and lease fencing each block the same paths first; that check is defense in depth.

## Other verification performed

- **Process-level smoke:** `opa migrate`, `opa bootstrap-owner` (and its refusal when an owner exists), `opa api`, and `opa worker` run as separate processes against real PostgreSQL; a submitted task completes and produces a notification.
- **Browser smoke:** `server/scripts/ui_smoke.py` runs API, broker, and worker as separate processes and drives headless Chromium: keyboard-only sign-in, DEMO badge, live reply via SSE, task state, retry, notifications, connecting a demo account (read only by default), allowing Gmail changes, a proposal that pauses the task and raises an approval count, an approval card with exact recipients and body approved by keyboard only, and completion after dispatch. Passes.

## Not verified / known limitations

- **Docker Compose has not been run.** Docker is not installed on the development machine. `deploy/compose.yaml` parses and its anchors resolve, but images have not been built.
- Compose now declares the broker, egress proxy, database roles, and isolated networks, but it has not been run, so the network half of T-NET-01 (the worker cannot reach any host except through the proxy) is unverified. The proxy's policy half is tested.
- Only the fixture provider exists. No Google API has been called.
- The Anthropic adapter has not been exercised against the live API (no key configured). It is code-complete but unverified.
- Frontend API types are hand-written; generating them from OpenAPI is scheduled for M6.
- SSE delivery polls the outbox every 500 ms rather than using `LISTEN/NOTIFY`. Correct but adds up to half a second of latency.
- Local-toolchain note: Rollup's native addon crashes under LingCode's embedded Node, so `web/package.json` overrides Rollup with its WebAssembly build. Standard Node installs are unaffected.

## External blockers

None for M1 or M2. M3 needs a Google Cloud OAuth client for live verification; fixture tests do not.
