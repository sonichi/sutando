# Tutorial delivery

The procedure lives in the `tutorial` skill: [`skills/tutorial/SKILL.md`](../skills/tutorial/SKILL.md).

- **Voice mode.** On "tutorial", "walk me through" or "show me what you can do", read
  `notes/first-time-tutorial.md` and deliver it one brief, voice-friendly section at a time,
  waiting for the user to try each step.
- **AG2 Space mode.** Right after the first-contact greeting in the owner's DM, the skill creates
  one "Getting started" HTML page (Room Commons) in that DM, a short AG2 Space tour with one thing
  to try per part (a Claude Code history import part only on a Claude Code core), and summons
  the owner to it. A marker
  under `<workspace>/state/tutorial/` keeps it to one page per room, so a re-link or reinstall
  refreshes the page instead of adding another.
