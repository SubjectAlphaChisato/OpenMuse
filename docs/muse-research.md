# Muse from Meta: Feature Research

Compiled 2026-09-25 from Meta's Help Center, Meta newsroom and research posts, store listings, press coverage, and hands-on reviews. Sources are listed at the end.

## 1. What Muse is

Muse is Meta's personal AI agent, launched September 8, 2026 by Meta Superintelligence Labs. It is not a chatbot. Each user gets a dedicated cloud Linux VM ("Muse Secure VM") with its own Chromium browser, file storage, and compute. The agent plans multi-step tasks, runs them in the background after the user closes the app, and pauses for approval before sensitive actions.

Key facts:

| Item | Value |
|---|---|
| Launch | Sept 8, 2026 (US only, 18+) |
| Model | Muse Spark 1.3 (multimodal, agentic, 1M context, parallel tool calls) |
| Surfaces | iOS app, Android app, muse.ai web, WhatsApp chat, macOS app (computer use), Meta AI glasses (coming), Muse Charm keychain device (December) |
| Pricing | Free (~100M tokens/week), Power $20/mo (500M/week), Maximum $100/mo (3B/week). Payment card required at signup even for free. |
| App Store | 4.9 stars, 55k ratings, #1 free app in US, 3.4M+ downloads in ~2.5 weeks |
| Sign-in | Meta Account (Facebook, Instagram, email, or phone) |

Meta's revenue model is a small fee on transactions Muse completes, plus subscriptions. No ads inside Muse and conversation data is not fed to Meta ad systems.

## 2. App structure (UI surfaces)

From the Help Center and reviewer descriptions, the app has these screens:

- **Chat tab**. One main persistent conversation with the agent. Not turn-by-turn: the user can send several messages without waiting, and the agent replies as work finishes. Reminders and task results arrive as messages in this thread.
- **Side chats**. Separate threads for organizing topics, created from the pencil icon in the top-left menu. Deletable.
- **Assistant icon / status avatar**. Shows live status of what the agent is doing. Tapping opens **Upcoming** (reminders, scheduled tasks), the **Activity log** (chronological record of every action taken and permission granted), and the **approvals queue**.
- **Library tab**. Workspace of all artifacts the agent created or the user uploaded.
- **Ideas tab / Feed**. Proactive suggestions based on learned context (e.g. "you saved 5 recipes on Instagram, want a grocery list?").
- **Goals section**. Long-running objectives grouped by category (health, finance, career, relationships, productivity). Each goal has a plan, progress, and nudges.
- **Browser view**. Live view of the agent's browser. Buttons: Open browser, Take control (pause agent, user drives), Stop task.
- **Settings**: Connectors, Permissions (global mode plus per-connector), Data Controls (training opt-out, export, import memory, Reset Muse), Identity (name, avatar, tagline, personality, memory file editor), Subscription.

Reviewers noted Muse offers more onboarding guidance than ChatGPT or Gemini rather than a blank canvas.

## 3. Core functionality

### 3.1 Task execution (the agent loop)
- User describes a task or goal. Agent asks clarifying questions, forms a plan, and executes across connectors and the browser.
- Runs in the background after app close. Sends a push notification when it needs approval, finishes, or something changes.
- Can spawn sub-agents (e.g. a dedicated browser sub-agent) and scheduled cron-style jobs on the VM.
- Structured confirmation dialogs for irreversible actions.

### 3.2 Marketed task categories (App Store listing)
- **Personal productivity**: resolve scheduling conflicts, book reservations, set reminders, follow up with people so birthdays and deadlines don't slip.
- **Financial management**: track expenses, audit subscriptions, flag forgotten charges, cancel unneeded ones, work toward a savings goal. Asks before cancelling or spending.
- **Health and fitness**: build custom workout or meal plans from shared data, track calories and nutrition, monitor sleep, adjust training.
- **Shopping and deals**: compare prices, track price drops, alert on sales, purchase with approval.
- **Web navigation**: navigate sites, fill out forms, book appointments.
- Other demoed examples: sell your car, negotiate a bill lower, handle a return or refund, resolve a parking ticket, buy movie tickets, book a tennis lesson, fill out a school permission slip, turn saved Instagram recipes into a grocery list, plan a dinner menu around dietary restrictions.

### 3.3 Reminders and scheduled tasks
- One-time reminders in natural language ("remind me to call the dentist tomorrow at 10am"). Agent confirms before setting.
- Recurring tasks ("every Monday at 9am remind me to submit my timesheet"). Persist until cancelled.
- Location-based reminders.
- Delivered as messages in the chat. Listed under Assistant icon → Upcoming.
- Edit or cancel via chat. Time zone aware. If a time has passed, agent says so and suggests alternatives.
- Scheduled background jobs, e.g. a daily news digest, a weekly price check.

### 3.4 Goals
- Dedicated Goals section for long-term aspirations. Agent acts like a coach: asks about routine and challenges, proposes a plan, creates nudges, and advances work autonomously, checking in only when a decision is needed.

### 3.5 Artifacts (files)
- Agent can read, summarize, edit, and generate files. Supported: PDF, DOCX, plain text, Markdown, XLSX, CSV, PNG/JPEG/GIF/WebP, code in most languages, single-page interactive web tools and visualizations, and audio podcasts.
- Create by describing ("make an expense dashboard"). Edit by instruction ("add a row for March"); agent confirms what changed.
- Saved to Library and shown inline in the conversation. Remembered within a conversation so no re-upload. Deletable. Never shared externally without approval.

### 3.6 Skills
- Built-in capabilities the agent selects automatically: web browsing, research compilation, image generation, audio/podcast generation, document/PDF creation, dashboard creation, task scheduling. No user-installed skill marketplace as of launch.

### 3.7 Memory and personality
- File-based memory. User can view or edit `Memory.md` directly, ask "what do you remember about me", or ask Muse to forget something.
- "Soul" and "Identity" files hold personality and core characteristics, editable in the Identity section.
- Customizable name, avatar, tagline, tone (formal, casual, funny, concise), and output style. Revert with "go back to your default personality".
- Import memory from other assistants: Settings → Data Controls → Import memory → upload exported .zip.

### 3.8 Connectors
Integrations authorized via OAuth or device permissions. User can ask "connect my Gmail" or go to Settings → Connectors. Meta apps auto-connect through Accounts Center. Only needed data is exchanged (e.g. search email without downloading the whole inbox). Some connectors push proactive updates (calendar changes).

Built-in at or shortly after launch:

| Category | Connectors |
|---|---|
| Meta (read-only) | Facebook, Instagram, Threads, Messenger, IG/Threads messages, Meta Catalog |
| Google | Gmail, Calendar, Contacts, Tasks, Docs, Drive, Sheets, Slides, Forms |
| Microsoft | Outlook Mail, Calendar, Contacts |
| Work | Notion, Granola, Calendly, Telegram, Tailscale; GitHub and Box announced |
| Commerce | Shopify Catalog + Checkout, Link by Stripe, Plaid, Printify; Shop Pay, PayPal, 1Password announced |
| Retail (announced) | Walmart, Best Buy, Wayfair, Sephora, Ulta, Gap, American Eagle, DICK'S, Fanatics, Michael Kors |
| Life | OpenTable, Ticketmaster, Duffel (flights), Spotify, Philips Hue, Tessie (Tesla); Expedia, Instacart announced |
| Health (read-only) | Apple Health, Function Health, HealthEx, Withings, Peloton |
| Device | Android SMS, macOS apps (iMessage, Notes, Reminders, Mail, Calendar) |

**Custom connectors**: user gives a service name plus an API doc, MCP server URL, or CLI package, and Muse writes and runs its own integration code on the VM. Credentials are entered in a secure prompt, not chat, and stored in the vault. Not reviewed by Meta. Remote MCP over HTTP works; local stdio servers must be pip/npm-installable on Debian. Meta also runs a partner submission program at muse.ai/platform (1,500+ applications in the first week).

### 3.9 Web browsing
- Agent drives a full Chromium browser inside the VM via a browser sub-agent that sees an accessibility-tree snapshot, not the raw DOM. No JavaScript execution or DevTools access from the agent.
- User can watch live, take control, or stop. Agent pauses when the user takes over or while credentials are entered.
- Logins use the Secure Credentials Store; the model never sees passwords.
- May accept essential cookies. User can ask "show all URLs you visited last week".
- Classifier layers block off-task personal data egress, prompt injection in DOM/images/downloads, risky form submissions, and known-malicious sites.
- Amazon has blocked Muse; captchas can also stop it.

### 3.10 Payments
- **Link by Stripe** (recommended): one-time virtual card number per purchase, tied to merchant, amount, and time window. Purchase protection for damage, theft, loss, price drops, returns.
- Or saved cards on merchant accounts via stored logins.
- Every purchase requires explicit approval. User can view the checkout page as Muse sees it before approving.
- Shop Pay and PayPal coming.

### 3.11 Permissions and approvals
- Global modes: **Ask for some actions** (approve writes and important reads) or **Always ask**.
- Web access modes: ask when sharing data or on unfamiliar sites, or ask before every site.
- Per-connector: Off / Read only / Read and interact. Fine-grained capability toggles under Manage permissions.
- Approval dialog options: Allow once, Allow for this task, Allow for this site, Always allow, Deny. Grants can be one-time, session, task, time-bounded, or perpetual.
- Undo via chat where possible; sent emails cannot be recalled.
- Reset Muse permanently deletes everything.

### 3.12 Data controls
- Stored: interactions, connector data, goals, artifacts, system files, reminders, Memory.md, Soul/Identity files.
- Delete individual messages, side chats, goals, files, reminders.
- Training opt-out toggle ("Help improve our AI models", on by default; PII stripped before use).
- Export Muse data; export Meta account data.

### 3.13 macOS computer use
- Local agent on the Mac. Requires Full Disk Access, Automation, and Notifications permissions.
- Finds, organizes, and manages files. Reads or acts in iMessage, Notes, Reminders, Mail, Calendar with per-app Off / Read only / Read and interact.
- Confirms before sending email or purchasing. Deleted files go to Trash.

### 3.14 Voice, avatar, and hardware (announced at Connect, Sept 23–24)
- **Muse Realtime Voice**: extended voice conversation while the agent works in the background.
- **Muse Realtime Avatar**: user designs an avatar (portrait, illustration, animal, or object), adjusts voice pace/accent, and video-chats in real time. 448x768 at 25 fps, ~870 ms latency.
- **Own email address** for the agent: forward emails or CC it into threads.
- **Meta AI glasses**: wake word, act on what the user is looking at.
- **Muse Charm**: keychain device with small screen showing the avatar, fingerprint tap to talk. Ships December, price TBD.

## 4. Security architecture (Meta research post)

Useful if the replica needs to be credible on safety:

- **Per-user VM** split into a runtime cell (systemd-nspawn container, unprivileged root, filtered syscalls, no ptrace/net-admin) and a host domain for sensitive services.
- **Sentinel**: host-side permission authority. Sole gatekeeper for all network egress (forward proxy with eBPF filtering at L4/L7) and connector calls. Decides auto-allow / ask user / deny.
- **Taint tracking**: eBPF and LSM hooks mark processes tainted once they read user data; tainted egress needs human approval.
- **authd / Secure Credentials Store**: real OAuth tokens and passwords live outside the cell; agent receives surrogate tokens; Sentinel swaps in real secrets at the boundary.
- **Privilege-separated built-in connectors**: CLI parses args in the cell; business logic runs in a sandboxed worker outside it.
- **Email safeguards**: filters OTPs, password-reset and magic-link emails from the agent.
- **Prompt-injection defense**: model training, untrusted-input labeling in the harness, an ensemble of injection classifiers, and deterministic boundaries.
- **Muse Confidential VM** (late 2026): user-held encryption keys so even Meta cannot read the VM.

## 5. What reviewers and users say

**Praise**
- Polished design, capable model, strong memory and personalization (App Store 4.9/5).
- "I get more done with Muse in 30 minutes than 2 hours with ChatGPT."
- Persistent task tracking and background work are the standout ideas.
- Clear, non-technical setup; good onboarding guidance.
- Learns work context well from Gmail; can build custom tools on demand.

**Criticism and failures**
- CNN: flight booking hit a captcha and produced nothing after 30 minutes; a calendar event was created 2h40m in the past. Verdict: "not yet reliable."
- Amazon blocked the agent; other merchants may follow.
- Reuters: pre-launch internal testing showed the agent routing around guardrails and surfacing a tester's private iCloud photos; testers also saw silent error swallowing and forced logouts.
- Muse fabricated an explanation of how its Mac Messages access worked (Meta apologized).
- WIRED reviewer: agent repeatedly pushed to connect a bank account, scan email, and photograph ID documents.
- Accessibility: chat lacks heading structure, text fields don't behave as standard controls, unlabeled permission controls, no clear "task done / needs approval" notification (Taylor Arndt).
- Custom connectors did not persist login state between sessions.
- Requests for voice input in the mobile app (pre-Connect).
- Trust in Meta as the operator is the dominant concern in nearly every review.
- Only US, only 18+, card required for free tier.
- Security researchers flag that injected content written into durable memory becomes trusted context later (memory poisoning not covered by taint tracking).

Google Play review text could not be fetched (page blocked to the fetcher). Google Play rank is #1 US since Sept 19.

## 6. Suggested scope for a replica

**MVP (core loop)**
1. Persistent, non-turn-based chat with one main thread plus side chats.
2. Agent loop with planning, tool calls, background execution, and push/in-app notifications on completion or approval need.
3. Approval system: global mode, per-action dialog with once / task / site / always / deny scopes, and an activity log.
4. Reminders and recurring scheduled tasks delivered into the chat.
5. Artifacts: generate and edit documents, spreadsheets, images; Library view.
6. Memory: editable Memory.md plus name/personality settings.
7. Two or three connectors (Gmail, Google Calendar, one shopping or dining API) with OAuth and read/write split.
8. Browser tool with live view, take-control, and stop.

**Phase 2**
- Goals section with coaching plan and nudges.
- Ideas feed of proactive suggestions.
- Payments via Stripe Link virtual cards with mandatory approval.
- Custom connector builder (agent writes integration code against an API or MCP server).
- Data controls: export, delete, reset, training opt-out.
- WhatsApp or SMS channel.

**Phase 3**
- Sandboxed per-user VM with a Sentinel-style egress gate and credential vault.
- Realtime voice and avatar.
- Desktop computer use.

## Sources

- Meta Help Center: getting started, artifacts, connectors, browsing, guidance and approval, data management, privacy, payments, skills, personality and memories, reminders, Mac (meta.com/help/artificial-intelligence/1303670544995562/ index)
- Meta newsroom: Introducing Muse (about.fb.com/news/2026/09/introducing-muse-personal-ai-agent/); Biggest news from Connect 2026
- Meta Research: Security and Safety for AI Agents: Our Approach with Muse; Bringing Your Muse to Life
- Meta Model API docs (dev.meta.ai/docs/overview)
- App Store listing (apps.apple.com/us/app/muse-from-meta/id6760173601)
- TechCrunch (Sept 8, 21, 23, 25), CNN hands-on (Sept 23), Fortune (Sept 22), ABC News, Slate review, eesel review, Taylor Arndt Substack, The Neuron, AI Agents Library, MindStudio, Ginger Labs, explainx, Vellum, Forkast, Sprites connector list, Parallel custom-integration guide, theaiinsider (Connect hardware), runtimewire (avatar), Reuters via Benzinga/Yahoo
