# Skills

Every capability the assistant has is a **skill**: a folder with a `SKILL.md` file. The harness shows the model a one-line catalog of skills at the start of each task; the model calls `skill__load` to get a skill's instructions and tools when a request needs them. This keeps prompts short as skills grow.

## What a skill is (and is not)

- A skill **organizes context**: instructions plus the tools that go with them.
- A skill **is not a permission**. What a tool may do is still decided by account access (off / read only / read and propose changes), the broker, and approvals. Loading the Gmail skill does not let the assistant send email; the owner's approval of the exact email does.
- If the model calls a tool from a skill it did not load, the harness loads that skill for it. A skill the owner turned off (Skills screen) is hidden from the model and its tools are refused.

## Built-in skills

| Skill | Tools | Needs |
|---|---|---|
| `gmail` | `gmail__search_messages`, `gmail__get_message`, `gmail__get_thread`, `gmail__send_message` | Gmail access |
| `calendar` | `calendar__list_calendars`, `calendar__list_events`, `calendar__get_event`, `calendar__create_event`, `calendar__update_event` | Calendar access |
| `drive` | `drive__search_files`, `drive__read_file` | Drive access |
| `notion` | `notion__search`, `notion__read_page`, `notion__query_data_source` | Notion access |
| `github` | `github__list_repos`, `github__search_issues`, `github__get_issue`, `github__read_file` | GitHub access |
| `files` | `artifact__*` | — |
| `memory` | `memory__remember`, `memory__suggest` | — |
| `automations` | `automation__propose`, `automation__list`, `automation__trigger_details` | — |
| `browser` | `browser__*` | the browser service |
| `daily-brief` | (uses `gmail`, `calendar`, `files`) | Gmail and Calendar access |

Always available, outside any skill: `ask_user`, `note`, `skill__load`, `connections__list`, `connections__request`.

## Accounts from the chat

`connections__request` lets the assistant ask to **connect** an account, **change** its access, **reconnect** one whose sign-in expired, or **disconnect** it. The request appears as a card in the conversation.

- Connecting, adding access, and disconnecting happen only when the owner confirms the card (`POST /api/connection-requests/{id}/confirm`, which needs the signed-in session and CSRF token). The assistant has no way to confirm.
- A change that only **reduces** access is applied at once and recorded on the card, because it can never widen what the assistant may do.
- The card lists the exact changes from validated fields. The assistant's `reason` is shown in quotes and never changes what is confirmed.
- Requests are single use, bound to the owner, expire after 24 hours, and a newer request for the same thing replaces an older one. A restore from backup expires open requests.
- When a provider must be asked (a first sign-in, or Google granting Drive), confirming opens the provider's consent screen; the sign-in returns to the chat and completes the card.

## Writing your own skills

Set `OPA_SKILLS_DIR` to a folder of skill folders. Your skills are **instruction-only**: they may combine built-in skills with `skills:` but may not declare `tools:`, so a skill file can never add code, tools, or access. Invalid skills stop the worker at start-up.

```
my-skills/
  weekly-review/
    SKILL.md
```

```markdown
---
name: weekly-review
title: Weekly review
description: "Review the past week's meetings and write a short summary document. Load when the owner asks for a weekly review."
summary: "A short review of last week's meetings."
skills: [calendar, files]
---
1. List last week's events (Monday to Sunday, owner's time zone).
2. Write one Markdown document named `Week of YYYY-MM-DD` with what happened and open follow-ups. Cite events in source_refs.
3. Reply with the document name and at most three sentences.
```

`description` is what the model reads; the optional `summary` is the one line shown to the owner on the Skills and accounts screen.

Rules checked when loading: `name` is lowercase letters, digits, or `-`, and matches the folder; `description` is at most 300 characters; instructions are at most 20,000 characters; names are unique; dependencies exist and have no cycles.
