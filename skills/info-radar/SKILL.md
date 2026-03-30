---
name: info-radar
description: "Monitor arXiv, GitHub, Hacker News, and tech news for topics the owner cares about. Surface discoveries via morning briefing or voice."
user-invocable: true
---

# Information Radar

Monitor external sources for topics the owner cares about. Surface notable findings.

**Usage**: `/info-radar [topic]`

ARGUMENTS: $ARGUMENTS

## Topics to Monitor

Maintain a watch list in `data/radar-topics.json`:

```json
{
  "topics": [
    {"name": "agentic AI", "keywords": ["agentic", "AI agent", "autonomous agent", "tool use"]},
    {"name": "personal AI", "keywords": ["personal AI", "personal assistant", "AI companion"]},
    {"name": "voice AI", "keywords": ["voice agent", "realtime voice", "speech-to-speech", "Gemini Live"]},
    {"name": "multi-agent", "keywords": ["multi-agent", "agent coordination", "AutoGen", "AG2", "CrewAI"]},
    {"name": "OpenClaw", "keywords": ["OpenClaw", "NemoClaw", "open agent skills"]},
    {"name": "Claude Code", "keywords": ["Claude Code", "claude-code", "Anthropic CLI"]},
    {"name": "computer use", "keywords": ["computer use", "desktop agent", "screen control", "GUI agent"]}
  ],
  "last_scan": null
}
```

The owner can add topics via voice: "add X to the radar" → update the JSON.

## Sources

### 1. arXiv (daily)
Search for papers matching topic keywords:
```bash
WebSearch "site:arxiv.org [keywords] [this week]"
```
Look for: new papers, high-citation preprints, notable authors in the field.

### 2. GitHub Trending (daily)
```bash
WebFetch "https://github.com/trending?since=daily" "List trending repos related to AI agents, voice AI, or personal AI"
```
Look for: new repos >100 stars, repos from known orgs (Anthropic, Google, OpenAI, Microsoft).

### 3. Hacker News (daily)
```bash
WebSearch "site:news.ycombinator.com [keywords] [this week]"
```
Look for: front-page posts about monitored topics, Show HN posts from competitors.

### 4. Tech News (daily)
General web search for recent news on monitored topics.

## Output

Generate a radar digest:

```
Information Radar — [date]

New papers:
- [Title] (arXiv:XXXX) — [1-line summary]. Relevant to: [topic]

Trending repos:
- [repo] ([stars]) — [description]. Relevant to: [topic]

Notable news:
- [headline] — [1-line summary]. Relevant to: [topic]

Nothing new: [topics with no hits]
```

## Delivery

- Write to `notes/radar-{date}.md` for reference
- Include highlights in morning briefing (step 6)
- If something urgent (competitor launch, major paper), notify immediately via `results/radar-alert-{ts}.txt`

## Scheduling

Run daily as part of the proactive loop or morning briefing. If ARGUMENTS specifies a topic, do an immediate deep scan on that topic only.

## Integration

- Morning briefing includes radar highlights
- Dynamic region can show radar alerts
- Owner can ask "what's new in [topic]" → triggers immediate scan
