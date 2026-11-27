"""What anyone but the owner is told about this agent's account and model, and the words that ask: one
policy for the core (switch_card_intercept) and every connector (ag2_sparrow.card_desk, a sync_from_src copy).

Owner 2026-11-26: a team member (a collaborator too) is told the model's name and nothing else, a guest to
ask the owner; the account (who is signed in, its email, plan or usage) is told to no one but the owner.
Standard library only, plus local_task_protocol (relative first, as the package bundles it).
"""
from __future__ import annotations

import re

IN_ROOM = "Switching the account or model isn't available in a room. Please ask me in our DM."
IN_ROOM_ZH = "房间里不能切换账号或模型，请到私聊里找我。"
MODEL_NAME = "I'm running on %s."
MODEL_NAME_ZH = "我现在用的模型是 %s。"
MODEL_UNKNOWN = "I can't tell which model I'm on right now."
MODEL_UNKNOWN_ZH = "我现在读不到自己在用的模型。"
ACCOUNT_OWNER_ONLY = "I only share account details with my owner."
ACCOUNT_OWNER_ONLY_ZH = "账号信息只告诉我的主人。"
SWITCH_OWNER_ONLY = "Only my owner can switch my account or model."
SWITCH_OWNER_ONLY_ZH = "只有我的主人能切换我的账号或模型。"
ASK_OWNER = "Please ask my owner."
ASK_OWNER_ZH = "请问我的主人。"

CJK = re.compile(r"[㐀-鿿]")
PICK_MAX = 80


def is_zh(words) -> bool:
    return bool(CJK.search(words or ""))


def not_owner_line(tier: str, kinds, switch: bool, zh: bool, model_name: str = "") -> str:
    """The one line for a sender who is not the owner: a guest to ask the owner; team that only the owner
    switches, the model's name (`model_name`, '' when not known) for a model question, else that the
    account is the owner's alone."""
    if tier != "team":
        return ASK_OWNER_ZH if zh else ASK_OWNER
    if switch:
        return SWITCH_OWNER_ONLY_ZH if zh else SWITCH_OWNER_ONLY
    if "model" in kinds:
        if model_name:
            return (MODEL_NAME_ZH if zh else MODEL_NAME) % model_name
        return MODEL_UNKNOWN_ZH if zh else MODEL_UNKNOWN
    return ACCOUNT_OWNER_ONLY_ZH if zh else ACCOUNT_OWNER_ONLY


def sender_tier(headers) -> str:
    """'team' or 'guest' for a sender who is not the owner, '' for the owner and any other tier. The
    bridge writes a collaborator as `access_tier: team` plus `collaborator: true`; any `collaborator` makes
    an owner tier team, as agent_card.owner_may_act refuses it."""
    try:
        from . import local_task_protocol as ltp
    except ImportError:
        import local_task_protocol as ltp
    tier = ltp.canonical_access_tier((headers or {}).get("access_tier"))
    collaborator = bool(str((headers or {}).get("collaborator") or "").strip())
    if tier == "team" or (tier == "owner" and collaborator):
        return "team"
    return "guest" if tier == "guest" else ""


# A pick named in a sender's own words, for any runtime: "switch to opus", "change the model to gpt-5",
# "换成 sonnet". Only the names of models, so "switch to dark mode" is not one.
_FAMILY = r"(?:opus|sonnet|haiku|fable|claude|gpt|codex|gemini|o\d|default)"
_PART = r"(?:opus|sonnet|haiku|fable|claude|gpt|codex|gemini|pro|flash|mini|max|high|low|medium|[-.\w]*\d[-.\w]*)"
_NAMED = _FAMILY + r"[-.\w\[\]]*(?:\s+" + _PART + r"[-.\w\[\]]*){0,2}(?:\s*\(1m context\))?"
_PICK = re.compile(r"^\s*(?:(?:hey|hi|ok|okay|please|pls|can you|could you)[\s,]+)*"
                   r"(?:(?:switch|change|move|go)(?:\s+(?:the|your))?(?:\s+(?:models?|core))?\s+to|use)\s+(?:the\s+)?"
                   r"|^\s*(?:请|帮我)?(?:切换|更换|换|改|切)(?:模型)?(?:到|成)\s*", re.I)
_PICKED = re.compile(_NAMED + r"(?:\s*(?:models?|模型))?(?:[\s,]*(?:please|pls|now|吧|一下))?\s*[.!?。！？]*\s*$", re.I)


def names_a_pick(text) -> bool:
    """Whether a short message picks a model by name ("switch to opus", "换成 sonnet")."""
    said = (text or "").replace("’", "'").strip()
    lead = _PICK.match(said) if said and len(said) <= PICK_MAX else None
    return bool(lead) and bool(_PICKED.fullmatch(said, lead.end()))


# Asked of this agent at any length, by anyone but the owner: its account (who is signed in, the email,
# the plan, the usage left) or its model. Each needs the agent ("you", "你") or a remaining-usage phrase.
_Q = r"(?:what|which|whose|what's|whats|tell me|share|show me|give me)"
_ACCOUNT_NOUN = (r"(?:accounts?|e-?mails?(?:\s+address(?:es)?)?|subscriptions?|billing|login|plan\s+tier|"
                 r"usage(?:\s+limits?)?|quotas?|credits?|rate\s+limits?)")
_ACCOUNT_EN = re.compile(
    r"\b" + _Q + r"\b[^.?!\n]{0,25}\b(?:your|ur)\s+(?:\w+\s+){0,2}?" + _ACCOUNT_NOUN + r"(?!\s+(?:of|for)\b)(?![\w-])"
    r"|\b(?:what|which)\s+(?:\w+\s+)?(?:accounts?|e-?mail(?:\s+address)?|plan|subscription|tier)\s+"
    r"(?:(?:are|r)\s+(?:you|u)\s+(?:on|using|signed|logged|running|subscribed)"
    r"|(?:do|did)\s+(?:you|u)\s+(?:use|have|sign|log|run|pay))\b"
    r"|\b(?:are|r)\s+(?:you|u)\s+(?:signed|logged)\s+in\b|\b(?:signed|logged)\s+in\s+as\b"
    r"|\b(?:your|ur)\s+(?:current\s+)?(?:subscription|billing|paid|pricing|pro|max|plus|team|enterprise|"
    r"claude|chatgpt|codex|gemini|google|anthropic|openai)\s+plan\b"
    r"|\b(?:are|r)\s+(?:you|u)\s+on\s+(?:a|an|the)\s+(?:\w+\s+){0,2}?plan\b"
    r"|\b(?:are|r)\s+(?:you|u)\s+(?:out\s+of|over|near|close\s+to|at|hitting)\s+(?:your\s+|the\s+)?"
    r"(?:usage|quota|credits?|(?:usage\s+|rate\s+)?limits?)\b"
    r"|\b(?:usage|quota|credits?)\s+(?:do\s+(?:you|u)\s+have\s+|(?:is|are)\s+(?:still\s+)?)?(?:left|remaining)\b"
    r"|\b(?:remaining|left)\s+(?:usage|quota|credits?)\b"
    r"|\bhow\s+much\s+(?:usage|quota|credits?)\b(?=[^.?!\n]*\b(?:left|remaining|you|u)\b)", re.I)
# Someone else's usage or account ("how much of my quota is left", "usage left on the GCP project").
_FOREIGN = re.compile(r"\b(?:my|our|their|his|her|mine)\b|\b(?:on|for|in|of)\s+(?:the|this|that)\s+(?!agent\b)\w", re.I)
_YOUR_ACCOUNT = re.compile(r"\b(?:your|ur|you|u)\b", re.I)
_ACCOUNT_ZH = re.compile(
    r"(?:你|您)[^。？?！!，,]{0,10}(?:账号|账户|帐号|帐户|邮箱|套餐|订阅|额度|用量|配额|余额|plan|email|quota|usage)"
    r"(?=[^。！!]*(?:什么|哪|多少|吗|么|是不是|几|？|\?|$))"
    r"|(?:额度|用量|配额|余额)[^。？?！!]{0,6}(?:还剩|剩下|剩多少|还有多少|够不够|用完)|还剩多少(?:额度|用量|配额)", re.I)
_MODEL_EN = re.compile(
    r"\b(?:what|which)\s+(?:\w+\s+){0,2}?(?:models?|llms?)\s+(?:(?:are|r)\s+(?:you|u)|(?:do|does|did)\s+(?:you|u)\s+"
    r"(?:use|run|have)|(?:is|'s)\s+(?:powering|behind|running)\s+(?:you|u|this\s+agent))\b"
    r"|\b" + _Q + r"\b[^.?!\n]{0,20}\b(?:your|ur)\s+(?:current\s+|underlying\s+|base\s+)?(?:model|llm)\b(?!\s+(?:of|for)\b)",
    re.I)
_MODEL_ZH = re.compile(
    r"(?:你|您)[^。？?！!，,]{0,10}(?:什么|哪个|哪一个|啥|哪种)\s*(?:模型|model|llm)"
    r"|(?:你|您)(?:的)?\s*(?:模型|model)\s*(?:是|叫)?\s*(?:什么|哪个|啥)", re.I)
# Advice about models in general, not the one this agent runs: "which model should you use for this".
_ADVICE = re.compile(r"\b(?:should|best|better|recommend|suggest|prefer)\b|应该|推荐|哪个好|更好", re.I)
_MINE_ZH = re.compile(r"我的|我们的")


def owner_only_kinds(text) -> frozenset:
    """{"account"} / {"model"} / both, when a message of any length asks about this agent's account
    (signed-in user, email, plan, usage) or its model; empty for anything else ("email me the report",
    "what's the usage of this API", "which model is best for coding")."""
    said = (text or "").replace("’", "'").strip()
    if not said:
        return frozenset()
    kinds = set()
    if (_ACCOUNT_EN.search(said) and (not _FOREIGN.search(said) or _YOUR_ACCOUNT.search(said))) \
            or (_ACCOUNT_ZH.search(said) and not _MINE_ZH.search(said)):
        kinds.add("account")
    if (_MODEL_EN.search(said) or _MODEL_ZH.search(said)) and not _ADVICE.search(said):
        kinds.add("model")
    return frozenset(kinds)
