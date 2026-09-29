---
name: daily-brief
title: "Daily brief"
description: "Prepare a brief of today's meetings and the email that needs attention, saved as a document. Load when the owner asks for a morning brief, a day plan, or 'what's on today'."
summary: "A morning brief of today's meetings and email that needs a reply."
skills: [gmail, calendar, files]
---
# Daily brief

A workflow skill: it uses Gmail, Calendar, and Files, which are loaded with it.

1. List today's events on the primary calendar (owner's time zone, from now to midnight).
2. Search email from the last 24 hours that is unread or from people in today's meetings. Read only what you need.
3. Write one Markdown document with `artifact__create_markdown`, named `Brief YYYY-MM-DD`:
   - **Today**: each meeting with time, title, and one line of context from related email.
   - **Needs a reply**: up to five threads, one line each, with why.
   - **Unknowns**: anything you could not check.
   List every message and event you used in `source_refs`.
4. Reply with three sentences at most and the document name. Do not send email or change events unless the owner asks.

If Gmail or Calendar is not connected, say which one and offer to connect it with `connections__request`.
