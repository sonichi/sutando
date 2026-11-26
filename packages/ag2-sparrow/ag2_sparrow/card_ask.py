"""Whether the owner's own words ask about the agent's account or model: one reading for every connector.

No list of whole sentences: a short message asks for a card when it names the matter (account, model) and
every other word in it is a question, a wish to change, the agent itself or a filler, in Chinese, English or
both. A word about anything else ("my balance", "for AWS", "欠费", "this paper") leaves it to the model.
"""
from __future__ import annotations

import re

ACCOUNT, MODEL = "account", "model"
ASK_MAX = 80

_W = r"(?<![a-z0-9])(?:%s)(?![a-z0-9])"
MATTERS = (
    (ACCOUNT, re.compile(r"账[号户]|帐[号户]|登录|登陆|" + _W % r"accounts?|e-?mail|(?:signed|logged|sign|log) ?in", re.I)),
    (MODEL, re.compile(r"模型|" + _W % r"models?", re.I)),
)
# Every word an ask may hold besides its matter. English as whole words, Chinese as they run.
_EN = (
    r"which|what|what's|whats|who|whose|how|is|are|r|am|do|does|did|can|could|would|will|tell|show|see|know|"
    r"me|you|u|your|yours|you're|youre|it|this|that|the|one|ones|now|currently|current|right|still|"
    r"using|use|used|on|in|with|as|to|at|switch|change|swap|pick|choose|set|another|other|different|"
    r"please|pls|hey|hi|ok|okay|so|and|or|also|then|i|mean|said|say|agent|running|run|there|here|"
    r"account|accounts|e-?mail|model|models|signed|logged|sign|log|login|"
    r"agy|antigravity|codex|claude|code|gpt[\w.-]*|o\d[\w.-]*|gemini[\w.-]*|opus|sonnet|haiku|fable"
)
_ZH = (
    r"请问|请|帮我|我要|我想|我说|我是说|我问|想|要|那|你们|你|您|自己|现在|目前|当前|正在|在|用的|使用|用|"
    r"登录的?|登陆的?|登的|是|的|了|吗|呢|啊|呀|吧|哦|嘛|哪一?个|哪些|什么|啥|谁|多少|有|能不能|能|可以|"
    r"看到|看见|看得到|看得见|看|告诉我|说|切换|切|换|更换|改|成|到|一下|个|别的|其他|另一?个|和|跟|与|还有|以及|"
    r"账[号户]|帐[号户]|模型"
)
_FILLER = re.compile(r"(?<![a-z0-9'])(?:%s)(?![a-z0-9'])|%s" % (_EN, _ZH), re.I)
_MARKS = re.compile(r"[\s,，、.。!！?？:：;；'\"“”‘’()（）/&+~-]+")


def asked(text) -> "set[str]":
    """The matters (ACCOUNT, MODEL) a whole, short message asks about; empty for anything else."""
    said = (text or "").strip()
    if not said or len(said) > ASK_MAX or said.startswith("/"):
        return set()
    matters = {name for name, words in MATTERS if words.search(said)}
    if not matters or _MARKS.sub("", _FILLER.sub(" ", said)):
        return set()
    return matters


def asks_account(text) -> bool:
    return ACCOUNT in asked(text)


def asks_model(text) -> bool:
    return MODEL in asked(text)
