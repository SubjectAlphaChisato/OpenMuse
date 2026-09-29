# Changelog

All notable changes are recorded here. Versions follow [Semantic Versioning](https://semver.org/).

## Unreleased (v0.2 connectors)

### Added
- **Read-only connectors:** Google Drive (search, and Docs/Sheets/Slides exported as Markdown, CSV, and text), Notion (search, pages with nested blocks, data-source queries), and GitHub through a GitHub App (repositories, issue and pull-request search, issues with comments, text files). All provider content is marked untrusted, and the browser's data-sharing approval covers it.
- A provider registry that declares each connector's capabilities and allowed modes; the API refuses a mode a capability does not support (`mode_not_supported`).
- Generic OAuth routes (`/api/connectors/{provider}/authorize`, `/api/oauth/{provider}/callback`) and a shared single-flight token manager used by every provider.
- The Connections screen lists every configured provider and shows only the modes each capability supports.

## 0.1.0 — unreleased

First release candidate of Open Personal Agent, implementing `docs/open-muse-implementation-requirements.md` v0.1.1.

### Added
- **Foundation (M1):** single-owner sign-in with CSRF and throttling; persistent chat with side conversations; durable background tasks with leases, fencing, budgets, cancel, resume, and retry; live updates over server-sent events with replay; Claude and scripted model adapters.
- **Approvals (M2):** connector broker; action ledger with exact-payload approvals, 24-hour expiry, duplicate-safe dispatch, and unknown-outcome reconciliation; separate database roles; encrypted credential vault; egress proxy for the model host.
- **Google (M3):** Gmail and Google Calendar through OAuth with PKCE, incremental scopes, single-flight refresh, revocation, and health checks.
- **Files and memory (M4):** uploads and versioned Markdown, CSV, and PDF files; attachment-bound approvals; explicit, editable memory with a confirmation gate.
- **Automations (M5):** one-time, daily, and weekday schedules with DST-safe timing, catch-up, and no overlap; calendar-change triggers with deduplication and self-edit suppression.
- **Browser and data (M6):** isolated public-web browser with live view, takeover, and a data-sharing approval; export with JSON Schemas; delete-all; backup and restore with post-restore safety rules.
- **Packaging:** `uv.lock` and hash-pinned image requirements; Dockerfiles that install only from the locks; `opa version`; generated frontend API types with a compile-time contract check; automated accessibility scan (axe-core); `scripts/release_check.py` running every release gate.

### Known limitations
See `IMPLEMENTATION_STATUS.md`. In short: Docker images and Compose have not been built on the development machine; Google and the Claude API have not been called live; the manual VoiceOver pass (A17b) is outstanding; Gmail attachments work only with the demo account.
