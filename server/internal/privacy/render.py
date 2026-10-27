"""What people read: the model's text with the real values put back under a spoiler."""

from internal.privacy.pseudonymizer import LABEL, LabelMap
from internal.telegram import Entity, Text, utf16_length


class TextBuilder:
    """Builds a message piece by piece, keeping the Bot API's UTF-16 offsets right."""

    def __init__(self, labels: LabelMap) -> None:
        self._labels = labels
        self._parts: list[str] = []
        self._entities: list[Entity] = []
        self._length = 0

    def plain(self, text: str) -> "TextBuilder":
        self._parts.append(text)
        self._length += utf16_length(text)
        return self

    def styled(self, kind: str, text: str, *, user_id: int | None = None) -> "TextBuilder":
        self._entities.append(Entity(kind, self._length, utf16_length(text), user_id))
        return self.plain(text)

    def revealed(self, text: str) -> "TextBuilder":
        """Text from the model: every label becomes its value, hidden until tapped."""
        position = 0
        for match in LABEL.finditer(text):
            value = self._labels.value(match.group())
            if value is None:
                continue
            self.plain(text[position : match.start()])
            self.styled("spoiler", value)
            position = match.end()
        return self.plain(text[position:])

    def build(self) -> Text:
        return Text("".join(self._parts), tuple(self._entities))
