# Learn from demonstration (moved verbatim from CLAUDE.md 2026-08-17)

## Learn from demonstration

When the user says "learn this", "remember my preference", "I always do it this way", or demonstrates a pattern:

1. **Extract the durable fact.** What is the user teaching? A preference, a workflow, a style choice, a correction?
2. **Classify it:**
   - *Preference* → update `<workspace>/.claude-sutando/projects/<slug>/memory/user_profile.md` (add to "Observed additions")
   - *Feedback/correction* → create or update a feedback core-memory file at `<workspace>/.claude-sutando/projects/<slug>/memory/feedback_*.md`
   - *Process/workflow* → save as a note in `notes/` with tag `[workflow, learned]`
3. **Update the core-memory index** `MEMORY.md` if a new file was created.
4. **Confirm briefly** what was learned: "Got it — I'll [do X] from now on."

Examples:
- "I prefer dark mode mockups" → update user_profile.md with design preference
- "When you draft emails, always start with the ask, not the context" → create feedback_email_style.md
- "Here's how I deploy: git push, then run make deploy, then check /status" → note with [workflow, learned]

