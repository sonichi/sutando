"""Presentation for the private Sutando onboarding overview."""
import base64
import html
import re
from pathlib import Path

PARTS = Path(__file__).resolve().parent.parent / "assets" / "dm-overview.html"  # lint-workspace-resolution: allow-repo-root
GUIDE = PARTS.with_name("dm-guide.html")
SLOT = re.compile(r"{{([A-Z_]+)}}")
TOPICS = ("Ecosystem", "Multiplayer", "Calls & Local Tools", "Commons & Tasks", "Sutando Guides")
EXAMPLES = {
    "team project": (
        "Help me connect an agent to review our project plan, then show me how to test its first reply.",
        "Summarize what the team decided about this project and suggest what another agent could check.",
        "Talk through our project kickoff. Start by asking what needs to be decided.",
        "Draft a Commons HTML page with our kickoff decisions, owners and open questions.",
    ),
    "engineering work": (
        "Show me how to bring in a second agent to review a technical plan.",
        "Summarize this technical discussion and list two questions another agent could investigate.",
        "Talk through the tradeoffs in this design with me. Ask a question before suggesting a solution.",
        "Draft a Commons HTML page with the design decision, alternatives and follow-up tests.",
    ),
    "design work": (
        "Show me how to bring in an agent to review a design brief.",
        "Summarize the design feedback in this thread and identify which questions are still open.",
        "Talk through the main design tradeoff with me before we pick a direction.",
        "Draft a Commons HTML page that groups the design feedback by theme.",
    ),
}
ROLE_CONTEXT = {
    "founder": "team project", "product manager": "team project",
    "project manager": "team project", "engineer": "engineering work",
    "software engineer": "engineering work", "developer": "engineering work",
    "designer": "design work", "product designer": "design work",
}
DEFAULT = (
    "Help me connect another agent and show me how to try its first reply.",
    "Summarize the decisions in this thread and suggest what another agent could check.",
    "Talk through this idea with me. Start by asking what I’m trying to solve.",
    "Draft a Commons HTML page with our meeting decisions and open questions.",
)
SURFACES = ("in your DM", "in a team room", "in a supported voice session", "in a room")
CHECKLIST = ('<a href="#tutorial-checklist" class="checklist-jump" '
             'onclick="document.getElementById(\'tutorial-checklist\').open=true">'
             'Try it step by step <span aria-hidden="true">↓</span></a>')


def _example(index, name, prompt):
    prompt_id = f"prompt-p{index + 1}"
    return (
        '<div class="prompt-box"><div>'
        f'<div class="prompt-meta">Try with {name} {SURFACES[index]}:</div>'
        f'<div class="prompt-text" id="{prompt_id}">{html.escape(prompt)}</div></div>'
        f'<button type="button" class="btn-copy" onclick="copyText(\'{prompt_id}\', this)">Copy</button></div>'
    )


def overview(style, body, name, roles):
    skin, parts = PARTS.read_text(encoding="utf-8").split("<!-- calls -->", 1)
    calls, parts = parts.split("<!-- sutando -->", 1)
    sutando, script = parts.split("<!-- script -->", 1)
    context = next((ROLE_CONTEXT[r.casefold()] for r in roles if r.casefold() in ROLE_CONTEXT), None)
    examples = EXAMPLES.get(context, DEFAULT)
    hint = f'<p class="preview-note">Examples for {context}.</p>' if context else ""
    tabs = "".join(
        f'<button type="button" class="tab-btn{" active" if i == 0 else ""}" '
        f'aria-current="{str(i == 0).lower()}" onclick="showStep({i})">'
        f'<span class="tab-num" aria-hidden="true">0{i+1}</span><span>{html.escape(topic)}</span></button>'
        for i, topic in enumerate(TOPICS)
    )
    header = (
        '<header class="overview-hero"><div class="hero-copy">'
        '<p class="guide-label"><span class="brand-mark" aria-hidden="true">S</span> Getting started</p>'
        f'{_heading(name)}'
        '<p class="subtitle">Set up the agents you use, bring them into rooms and calls, '
        'and keep shared work in Commons.</p>'
        f'{hint}</div><div class="guide-nav"><nav class="steps-tabs" aria-label="Guide topics">'
        f'{tabs}</nav>{CHECKLIST}</div></header>'
    )
    prefix, rest = body.split('<p class="subtitle">Interactive overview', 1)
    rest = rest.split("</header>", 1)[1]
    calls = calls.replace("<!-- calls-example -->", _example(2, name, examples[2]))
    rest = re.sub(r'<section class="tour-card" id="step-2">.*?</section>', lambda _: calls.strip(), rest,
                  count=1, flags=re.S)
    def replace_example(match):
        found = re.search(r'id="prompt-p([1-4])"', match[0])
        if not found:
            return match[0]
        index = int(found[1]) - 1
        return _example(index, name, examples[index]) if index != 2 else match[0]

    rest = re.sub(r'<div class="prompt-box">.*?</button>\s*</div>', replace_example, rest, flags=re.S)
    rest = rest.replace('onclick="showStep(0)">Return to Ecosystem ↺',
                        'onclick="showStep(4)">Explore Sutando Guides →')
    rest = rest.replace("</section>\n</div>\n\n<script>", f"</section>\n{sutando}\n</div>\n\n<script>", 1)
    rest = rest.replace('const stepNames = ["Ecosystem", "Multiplayer", "Calls & Local Tools", "Commons & Tasks"];',
                        'const stepNames = ["Ecosystem", "Multiplayer", "Calls & Local Tools", '
                        '"Commons & Tasks", "Sutando Guides"];')
    rest = rest.replace("</script>\n<noscript>", f"{script}</script>\n<noscript>", 1)
    rest = rest.replace("This overview works as a reference without JavaScript.",
                        "All five topics are shown when scripts are off.")
    for key in ("coach", "writing"):
        rest = re.sub(rf"^    {key}: .*?\n", "", rest, flags=re.M)
    return style + skin, prefix + header + rest


def _heading(name):
    heading = GUIDE.read_text(encoding="utf-8").split("<h1>", 1)[1].split("</h1>", 1)[0]
    return "<h1>" + heading.replace("{{AGENT_NAME}}", name) + "</h1>"


def render_guide(head, marker, name, roles, prompts):
    context = next((ROLE_CONTEXT[r.casefold()] for r in roles if r.casefold() in ROLE_CONTEXT), None)
    cases = EXAMPLES[context] if context else (
        "Help me connect another agent and test its first reply.",
        "Help me summarize the decisions from a project room. Ask me which room I mean if needed.",
        "Help me set up a voice conversation for a kickoff, if my agent and devices support it.",
        "Help me draft a Commons page for kickoff decisions, owners and open questions. Ask where it belongs.",
    )
    hints = ""
    if roles:
        hints = f'<p class="preview-note">Picked for: {html.escape(", ".join(roles))}</p>'
    if context:
        hints += f'<p class="preview-note">Examples for {html.escape(context)}.</p>'
    quick = {
        "DM": "What can you do for me today?",
        "COMMONS": "Help me make a team offsite plan in Commons. Ask me which room to use before sharing it.",
        "TEAMWORK": "Research three note-taking apps for a small team, then ask another agent to review your answer if permitted.",
        "APPS": "Connect my Google Calendar.",
    }
    finale = prompts.get("finale")
    slots = {
        "PAGE_HEAD": head,
        "OWNER_MARKER": marker,
        "AGENT_NAME": name,
        "SETUP_CONTEXT": hints,
        "APP_STORE_QR": base64.b64encode(GUIDE.with_name("dm-app-store-qr.png").read_bytes()).decode("ascii"),
        "FINALE_CONTROL": (f'<button type="button" class="btn-nav primary guide-ask" '
                           f'data-draft="{html.escape(finale)}" hidden>Ask for a first task</button>' if finale else ""),
        **{f"TOPIC_{label}": html.escape(prompt)
           for label, prompt in zip(("CONNECT", "TEAM", "CALL", "COMMONS"), cases)},
        **{f"DRAFT_{label}": html.escape(prompts.get(label.lower(), fallback)) for label, fallback in quick.items()},
    }
    return SLOT.sub(lambda match: slots[match[1]], GUIDE.read_text(encoding="utf-8"))
