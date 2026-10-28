"""The answer the model gives the person who asked, through the strict ``submit_answer`` tool."""

from dataclasses import dataclass
from typing import Any, Literal

from internal.agent.tools import nullable, strict_object
from internal.privacy.pseudonymizer import LabelMap
from internal.privacy.rich import RichMessageBuilder
from internal.telegram import RichMessage
from internal.words import Words

#: The parts of an answer about a case, in the order people read them (``case-answer``, the composition).
CASE_PARTS = ("client_saw", "cause", "whose_error", "tell_client_now", "next_steps")

SUBMIT_ANSWER_SCHEMA = strict_object(
    {
        "kind": {
            "type": "string",
            "enum": ["case", "reply", "question"],
            "description": "case: the answer about a client's case, in its five parts. reply: any other answer "
            "(a question about the product, a follow-up, the list of cases). question: what you need to know "
            "before you can go on, for example which client the case is about.",
        },
        "client_saw": nullable("string", "kind case: what the client saw, in one sentence."),
        "cause": nullable("string", "kind case: the cause in plain words, one or two sentences; what is an "
                                    "assumption is marked so."),
        "whose_error": nullable("string", "kind case: whose error it is, in one sentence: the organization's, "
                                          "the client's, a third party's, or not known."),
        "tell_client_now": nullable("string", "kind case: what can be told to the client now, one or two "
                                              "sentences or a short list."),
        "next_steps": nullable("string", "kind case: what happens next and who does it, in one sentence."),
        "text": nullable("string", "kind reply or question: the text, at most five sentences or a short list."),
        "attachments": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Ids of pictures to show with the answer: att_N of the request, img_N of the sources.",
        },
    }
)  # fmt: skip


@dataclass(frozen=True)
class Answer:
    kind: Literal["case", "reply", "question"]
    parts: dict[str, str]
    text: str | None
    attachments: tuple[str, ...]

    @classmethod
    def from_input(cls, data: dict[str, Any]) -> "Answer":
        """Raises ``ValueError`` with what is missing."""
        kind = data.get("kind")
        if kind == "case":
            missing = [part for part in CASE_PARTS if not (data.get(part) or "").strip()]
            if missing:
                raise ValueError(f"An answer about a case needs every part; missing: {', '.join(missing)}.")
            parts = {part: data[part].strip() for part in CASE_PARTS}
            text = None
        elif kind in ("reply", "question"):
            if not (data.get("text") or "").strip():
                raise ValueError(f"An answer of kind {kind} needs text.")
            parts, text = {}, data["text"].strip()
        else:
            raise ValueError("kind is case, reply or question.")
        return cls(kind, parts, text, tuple(data.get("attachments") or ()))

    def render(self, labels: LabelMap, words: Words) -> RichMessage:
        builder = RichMessageBuilder(labels)
        if self.text is not None:
            return builder.markdown(self.text).build()
        for part in CASE_PARTS:
            builder.heading(words.say(f"part_{part}")).markdown(self.parts[part])
        return builder.build()
