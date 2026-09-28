---
name: brainstorm
description: Use BEFORE any creative work — building a feature, scaffolding a project, designing a component. Forces scope clarification, requirement gathering, and a tiny design sketch before code is written. Without this, vague prompts produce vague code.
---
Use BEFORE writing any code, scaffolding any project, or building any
feature. This is the cheap-and-loud "stop, scope, design, then build"
gate. Do NOT skip just because the request looks simple — simple
projects are where unexamined assumptions cause the most wasted work.

## The discipline

1. **Read the request literally.** "Build an iOS app" is not a spec.
   "Add dark mode" is not a spec. The request is the START of the
   conversation, not the end.

2. **Identify what's missing.** What does success look like? Who is the
   user? What constraints exist (budget, time, deploy creds, hardware)?
   What's in scope vs explicitly out?

3. **Ask ONE question at a time** via AskUserQuestion. Prefer multiple
   choice over open-ended. Lead with the MOST foundational unknown —
   the answer should reshape every later question.

4. **Flag scope risk early.** If the request describes multiple
   independent subsystems (auth + storage + billing + analytics), STOP
   and propose decomposing into separate projects. Don't refine details
   of a thing that needs to be broken apart.

5. **Propose 2-3 approaches with tradeoffs.** Lead with your
   recommendation and why. Present alternatives concretely, not as
   handwaving ("or we could do it differently").

6. **Present the design in sections.** Get approval section by section.
   Sized to complexity: a single screen counter app is 5 sentences; a
   billing service is several paragraphs per section.

7. **Show the constraints honestly.** What can't be done? What's the
   ceiling (e.g. "no Apple Developer creds → simulator-only ceiling")?
   What's a hard-stop on the agent's autonomy?

## Anti-patterns

- Starting to scaffold before asking "what's the actual product?"
- Asking 4 questions in one message (overwhelm; the user defaults to
  the safest answer instead of the right one)
- Burying the recommendation in equal-weight alternatives
- Treating "yeah" or "do it" as license to skip remaining design
  questions — those phrases approve the LAST thing you proposed, not
  the whole rest of the road
- Pretending you know what the user wants when you don't

## When you HAVE finished brainstorming

Output: a design the user has explicitly approved, with constraints
acknowledged. Then move to implementation. Use writing-plans if the
work is multi-step and benefits from a written plan; otherwise just
execute with TodoWrite as the live plan.

## When NOT to use this

- User explicitly says "skip the design, just do X" — honor that, ask
  a single clarifying question if X is ambiguous, then execute
- One-line bugfix where the change is obvious from the code
- Question-answering tasks (no code being written)
- Continuing work where the design was already approved earlier in
  the conversation
