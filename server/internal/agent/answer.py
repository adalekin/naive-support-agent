"""The answer the model gives the person who asked, through the strict ``submit_answer`` tool."""

from dataclasses import dataclass
from typing import Any, Literal

from internal.agent.tools import nullable, strict_object
from internal.privacy.pseudonymizer import LabelMap
from internal.privacy.render import TextBuilder
from internal.telegram import Text
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
        "client_saw": nullable("string", "kind case: what the client saw."),
        "cause": nullable("string", "kind case: the cause in plain words; what is an assumption is marked so."),
        "whose_error": nullable("string", "kind case: whose error it is: the organization's, the client's, "
                                          "a third party's, or not known."),
        "tell_client_now": nullable("string", "kind case: what can be told to the client now."),
        "next_steps": nullable("string", "kind case: what happens next and who does it."),
        "text": nullable("string", "kind reply or question: the text."),
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

    def render(self, labels: LabelMap, words: Words) -> Text:
        builder = TextBuilder(labels)
        if self.text is not None:
            return builder.revealed(self.text).build()
        for index, part in enumerate(CASE_PARTS):
            if index:
                builder.plain("\n\n")
            builder.styled("bold", words.say(f"part_{part}")).plain("\n").revealed(self.parts[part])
        return builder.build()
