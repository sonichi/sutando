"""A question to the owner: one model, and the only place that turns it into words.

Every way of asking the owner (the decision card, the plain text sent where there are no cards,
the reminder, the pointer to a card) is rendered here from one `Question`, so the same rules
hold for all of them and are tested once:

- a known set of answers is a control: `options` become the card's buttons, and prose that
  lists its answers without options is refused at `validate` (a caller with a real free-text
  question says so);
- the visible text is the headline (one sentence), the why, and the choices. The machine's
  name, the question's row and any other bookkeeping are `details`: they sit below the card's
  first blank line, where the card folds them, and never appear in the text or the reminder;
- nothing tells the owner to keep a record: the asker's store resolves the question from a
  button or from the owner's own words;
- every text ends with how to answer, and everything is passed through `owner_text.redact`.

Stores and transports stay with the caller: this module imports no skill and sends nothing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from owner_text import DEFAULT_AGENT_NAME, FOLDED, named, redact

REF_KEYS = ("label", "url", "title", "state", "state_label", "group")
REF_STATES = ("ready", "waiting", "open", "merged", "closed")
MAX_REFS = 20
LABEL_MAX = 40
ANSWER_HOW = "Reply here in your own words."
ANSWER_HOW_OPTIONS = "Reply here with the letter, or in your own words."

# A list of answers written as prose: numbered or lettered items, or an "Options:" lead.
_LISTED = re.compile(r"\(1\).*\(2\)|(?:^|\s)1[.)]\s.*\s2[.)]\s|\(a\).*\(b\)|(?:^|\s)a\)\s.*\sb\)\s|\boptions?\s*:",
                     re.IGNORECASE | re.DOTALL)
# A two-way question: "yes/no", or a question sentence that offers "X or Y?".
_TWO_WAY = re.compile(r"\b(?:yes\s*/\s*no|y\s*/\s*n)\b|\bor\b[^.?!]*\?", re.IGNORECASE)
_SENTENCE_END = re.compile(r"(?<=[.?!])\s+")


class QuestionError(ValueError):
    """The question cannot be asked as given; the message says what to pass instead."""


def enumerated_choice(question: str, context: Optional[str] = None) -> str:
    """"listed" when the text spells out its answers as a list, "two-way" when the question
    offers two, "" for an open question. The context is read only for a list."""
    if _LISTED.search(f"{question or ''}\n{context or ''}"):
        return "listed"
    return "two-way" if _TWO_WAY.search(question or "") else ""


def split_headline(text: str) -> Tuple[str, str]:
    """(the first sentence of the first line, everything after it)."""
    first, _, below = (text or "").strip().partition("\n")
    head, *more = _SENTENCE_END.split(first.strip(), maxsplit=1)
    return head, "\n".join(p for p in (*more, below.strip()) if p)


@dataclass(frozen=True)
class Option:
    id: str
    label: str
    effect: str = ""
    note: str = ""
    primary: bool = False
    destructive: bool = False


@dataclass(frozen=True)
class Ref:
    label: str
    url: str
    title: str = ""
    state: str = ""
    state_label: str = ""
    group: str = ""

    def to_wire(self) -> Dict[str, str]:
        return {k: v for k in REF_KEYS for v in [getattr(self, k)] if v}

    def line(self) -> str:
        return f"{self.label} — {self.title}: {self.url}" if self.title else f"{self.label}: {self.url}"


def parse_ref(text: str) -> Ref:
    """`label=#123;url=https://…;title=…;state=ready;state_label=…;group=…` (label and url required)."""
    got: Dict[str, str] = {}
    for part in filter(str.strip, (text or "").split(";")):
        key, sep, value = part.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or key not in REF_KEYS or not value or key in got:
            raise QuestionError(f"a ref is 'label=…;url=https://…' plus optional "
                                f"{', '.join(REF_KEYS[2:])}, each once: {part.strip()!r}")
        got[key] = value
    return check_ref(Ref(**{k: got.get(k, "") for k in REF_KEYS}))


def check_ref(ref: Ref) -> Ref:
    if not ref.label or not ref.url:
        raise QuestionError("a ref needs both label and url")
    if not ref.url.startswith("https://") or len(ref.url) == len("https://"):
        raise QuestionError(f"a ref url must be https: {ref.url!r}")
    if ref.state and ref.state not in REF_STATES:
        raise QuestionError(f"a ref state is one of {', '.join(REF_STATES)}: {ref.state!r}")
    return ref


@dataclass(frozen=True)
class Question:
    """`headline` is the one sentence asked; `context` the why; `options` the answers (none for
    an open question), `proposed` the id of the one the asker recommends and `reason` why;
    `refs` what it is about (an option's `effect` is what choosing it does, its `note` the line a
    card shows under it); `details` bookkeeping a person never needs to read; `asker` the
    agent's display name; `intro` one paragraph a store adds the first time."""
    headline: str
    context: str = ""
    options: Tuple[Option, ...] = ()
    refs: Tuple[Ref, ...] = ()
    details: Tuple[str, ...] = ()
    asker: str = ""
    reason: str = ""
    proposed: str = ""
    intro: str = ""
    asked: str = ""
    extra_subject: Dict[str, Any] = field(default_factory=dict, compare=False)

    @classmethod
    def from_text(cls, question: str, context: Optional[str] = None, **fields: Any) -> "Question":
        """The first sentence is the headline; the rest of the question leads the context."""
        head, rest = split_headline(question)
        why = "\n\n".join(p for p in (rest, (context or "").strip()) if p)
        for key in ("options", "refs", "details"):
            if key in fields:
                fields[key] = tuple(fields[key] or ())
        return cls(headline=head, context=why, asked=(question or "").strip(), **fields)

    # ---- validation, at the one boundary -------------------------------------------------

    def problems(self, open_question: bool = False, proposes: bool = False) -> List[str]:
        """What stops this question being asked, each with what to pass instead. `open_question`:
        the answer really is free text. `proposes`: the caller proposes an action to approve, so a
        yes-or-no wording already has its control."""
        out: List[str] = []
        if not self.headline.strip():
            out.append("the question is empty: pass the one sentence you are asking")
        labels = [o.label.strip().lower() for o in self.options]
        for o in self.options:
            if not o.label.strip() or len(o.label.strip()) > LABEL_MAX:
                out.append(f"an option label is 1 to {LABEL_MAX} characters, got {o.label!r}: "
                           "put what it does in the option's effect")
            if o.destructive and not o.effect.strip():
                out.append(f"the destructive option {o.label!r} needs an effect: say what it removes")
        if len(set(labels)) != len(labels):
            out.append("two options share a label: give each answer its own")
        if len(self.refs) > MAX_REFS:
            out.append(f"at most {MAX_REFS} refs, got {len(self.refs)}: group them or link one page")
        for ref in self.refs:
            try:
                check_ref(ref)
            except QuestionError as e:
                out.append(str(e))
        if self.proposed and self.proposed not in {o.id for o in self.options}:
            out.append("the proposed answer is not one of the options: name an option's own label")
        kind = "" if open_question or self.options else enumerated_choice(self.asked or self.headline,
                                                                          self.context)
        if kind == "listed" or (kind == "two-way" and not proposes):
            out.append("this question lists its answers in prose, so it would be asked as plain text with "
                       "no buttons: pass one option per answer and keep the context for the why (or say "
                       "it is an open question when the answer really is free text)")
        return out

    def validate(self, open_question: bool = False, proposes: bool = False) -> "Question":
        found = self.problems(open_question, proposes)
        if found:
            raise QuestionError("; ".join(found))
        return self

    # ---- what every rendering shares -----------------------------------------------------

    @property
    def who(self) -> str:
        return named(self.asker, DEFAULT_AGENT_NAME)

    def lead(self) -> str:
        return f"{self.who} asks you to decide" if self.options else f"{self.who} has a question for you"

    def _why(self) -> List[str]:
        out = [self.context.strip()] if self.context.strip() else []
        label = next((o.label for o in self.options if o.id == self.proposed), "")
        if self.reason.strip() and label:
            out.append(f"Why {label}: {self.reason.strip()}")
        return out

    # ---- the decision card ---------------------------------------------------------------

    def card_title(self) -> str:
        return self.lead()

    def card_actions(self) -> List[Tuple[str, str]]:
        return [(o.id, o.label) for o in self.options]

    def card_message(self) -> str:
        """The headline alone on the first line; the why, then the details, below it. The
        buttons carry the choices, so they are not written out again."""
        parts = [self.headline.strip(), *self._why()]
        if self.intro.strip():
            parts.append(self.intro.strip())
        if self.details:
            parts.append("\n".join(d for d in self.details if str(d).strip()))
        return redact("\n\n".join(parts))

    def card_subject(self) -> Dict[str, Any]:
        """The structured half of the card: what each button does, the references as rows, the
        recommended and the primary button. `FOLDED`: a text-only reader gets the headline."""
        subject: Dict[str, Any] = {**self.extra_subject, FOLDED: True,
                                   "choices": {o.id: o.effect for o in self.options}}
        if self.proposed:
            subject["default"] = self.proposed
        if self.refs:
            subject["refs"] = [r.to_wire() for r in self.refs]
        effects = {o.id: o.note for o in self.options if o.note}
        if effects:
            subject["effects"] = effects
        styles = {o.id: "primary" for o in self.options if o.primary}
        if styles:
            subject["styles"] = styles
        return subject

    # ---- plain text, for a channel with no cards -----------------------------------------

    def text(self) -> str:
        """Lead, headline, why, the choices as a short lettered list, the references as
        labelled links, and how to answer. No row, no machine name."""
        lines = [f"{self.lead()}:", "", self.headline.strip()]
        for para in self._why():
            lines += ["", para]
        if self.options:
            lines.append("")
            lines += [f"{chr(ord('A') + i)}. {o.label}" + (f" — {o.effect}" if o.effect else "")
                      for i, o in enumerate(self.options)]
        if self.refs:
            lines += ["", *(r.line() for r in self.refs)]
        lines += ["", ANSWER_HOW_OPTIONS if self.options else ANSWER_HOW]
        if self.intro.strip():
            lines += ["", self.intro.strip()]
        return redact("\n".join(lines)) + "\n"

    # ---- follow-ups ----------------------------------------------------------------------

    def reminder(self) -> str:
        """One sentence about the same question; the card, where there is one, has the buttons."""
        return redact(f"{self.who} is still waiting for your answer: {self.headline.strip()} {ANSWER_HOW}")

    def pointer(self) -> str:
        """For a card that may not have arrived: the question, and an answer that always works."""
        return redact(f"{self.who} sent you a question as a card in your s.space DM, and it may not have "
                      f"arrived there.\n\n“{' '.join(self.headline.split())}”\n\n"
                      "If the card is not there, reply here in your own words.") + "\n"


def waiting_digest(titles: Sequence[str], snippets: Optional[Sequence[str]] = None, shown: int = 5) -> str:
    """The reminder for several open questions: their headlines, and how to answer."""
    n = len(titles)
    lines = [f"{n} question{'s are' if n != 1 else ' is'} still waiting for your answer:", ""]
    for i, title in enumerate(list(titles)[:shown]):
        lines.append(f"• {title}")
        snippet = (snippets[i] if snippets and i < len(snippets) else "") or ""
        if snippet:
            lines.append(f"  ↳ {snippet}")
    if n > shown:
        lines.append(f"…and {n - shown} more")
    lines += ["", ANSWER_HOW]
    return redact("\n".join(lines))


def host_detail(host: str) -> str:
    """The machine a question came from, for the details only."""
    return f"From your Mac named {host}" if (host or "").strip() else ""


def row_detail(link: Optional[str]) -> str:
    """Where the asker keeps the question, for the details only."""
    return f"Record: {link}" if (link or "").strip() else ""


def refs_from(items: Iterable[Any]) -> Tuple[Ref, ...]:
    """Refs from Ref objects or {label, url, …} mappings, each checked."""
    return tuple(check_ref(r if isinstance(r, Ref) else Ref(**{k: str(r.get(k) or "") for k in REF_KEYS}))
                 for r in items or ())
