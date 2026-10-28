# Changelog

All notable changes are recorded here. Versions follow [Semantic Versioning](https://semver.org/).

## Unreleased (CRM phase 1)

### Added
- **Accounts and permissions, phase 1** (docs/crm/accounts-schema.md): `organizations`; `owners` becomes `users` with `role` (admin / advisor / partner), `status`, `partner_id`, and CHECK constraints; minimal `partners` and `clients`; `client_assignments` with one active primary advisor per client (partial unique index). `opa.crm.accounts` holds the rules the database cannot: advisor visibility is derived from assignments, a new primary ends the old one, and deactivating a user signs them out at once, refuses while they hold clients, and never removes the last admin. Existing owners become active admins of a default organization.
- Reserved for shared conversations: `conversation_members` and `messages.author_user_id`.
- **Accounts, step 2:** invitations (link shown once, stored hashed, 7-day expiry, single use, optional initial client assignments) and account requests (`invite`, `change_role`, `assign_client`, `transfer_client`, `unassign_client`, `suspend`, `reactivate`, `deactivate`, `revoke_sessions`). Adding access waits for an admin's confirmation; an admin's own reduction applies at once; advisors can ask but never confirm; partner users can do neither. Sign-in and every request refuse suspended or deactivated accounts. New endpoints: `/api/team`, `/api/clients`, `/api/partners`, `/api/account-requests`, `/api/invitations`. `/api/me` now returns `name` and `role`.

## Unreleased (v0.2 connectors)

### Added
- **Skills harness:** every capability is a skill with a `SKILL.md` (instructions, tools, the account it needs). The model sees a short catalog and loads skills on demand with `skill__load`; loaded skills survive restarts. A daily-brief workflow skill shows how skills combine. Owners can add instruction-only skills with `OPA_SKILLS_DIR` and turn skills off on the new Skills screen. See `docs/skills.md`.
- **Sharing:** share chosen messages of a conversation as a read-only snapshot link that anyone can open without signing in. Only a hash of the link is stored; links expire (1, 7, or 30 days) and can be turned off; the page has no scripts or cookies and is kept out of search engines. Delete-all removes shared links and a restore turns them off.
- **Chat-first app:** the header shows only the assistant, a count of waiting approvals, and a Menu. Account cards and approvals appear inside the conversation. The Connections screen is gone; Skills and accounts shows access read-only. The chat opens with suggestions.
- **Accounts from the chat:** `connections__list` and `connections__request` let the assistant ask to connect, change, reconnect, or disconnect an account. The request is a card in the chat; connecting, adding access, and disconnecting happen only when the owner confirms it, and reductions apply at once. Provider sign-ins started from a card return to the chat.
- **Read-only connectors:** Google Drive (search, and Docs/Sheets/Slides exported as Markdown, CSV, and text), Notion (search, pages with nested blocks, data-source queries), and GitHub through a GitHub App (repositories, issue and pull-request search, issues with comments, text files). All provider content is marked untrusted, and the browser's data-sharing approval covers it.
- A provider registry that declares each connector's capabilities and allowed modes; the API refuses a mode a capability does not support (`mode_not_supported`).
- Generic OAuth routes (`/api/connectors/{provider}/authorize`, `/api/oauth/{provider}/callback`) and a shared single-flight token manager used by every provider.
- The Connections screen lists every configured provider and shows only the modes each capability supports.

### Fixed
- After a provider sign-in, the notice names the right provider (it always said Google), and the add-account list no longer repeats each provider's name.

## 0.1.0 — unreleased

First release candidate of OpenMuse, implementing `docs/open-muse-implementation-requirements.md` v0.1.1.

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
