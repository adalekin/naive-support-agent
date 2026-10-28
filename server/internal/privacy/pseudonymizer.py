"""Personal data stays out of the model: it sees labels, people see the values.

Emails and phone numbers are found by their shape in any text. Names and payment data are
found by the fields of what the sources return, listed by the installation; inside free text
of a client they are not recognised and reach the model as they are.
"""

import ast
import json
import re
from typing import Any

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE = re.compile(
    r"\+\d[\d\s\-()]{8,}\d"
    r"|(?<![\w+])[78][\s-]?\(?\d{3}\)?[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}(?!\w)"
)
#: How a label looks to the model; unlikely in real data and easy to find again.
LABEL = re.compile(r"<<([a-z]+)_(\d+)>>")


def _python_repr(text: str) -> Any:
    """What a server prints with Python's repr: literals as they are, every call or name as its source text.

    Rows of a database come with ``datetime.datetime(...)`` and ``Decimal('1.5')`` in them, which
    ``ast.literal_eval`` refuses; nothing here is evaluated.
    """
    return _literal(ast.parse(text, mode="eval").body)


def _literal(node: ast.expr) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.List | ast.Tuple | ast.Set):
        return [_literal(item) for item in node.elts]
    if isinstance(node, ast.Dict):
        return {_literal(key): _literal(value) for key, value in zip(node.keys, node.values, strict=True)}
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub) and isinstance(node.operand, ast.Constant):
        return -node.operand.value
    return ast.unparse(node)


class LabelMap:
    """Label to value for one case. The same value always gets the same label."""

    def __init__(self, labels: dict[str, str] | None = None) -> None:
        self._values: dict[str, str] = dict(labels or {})
        self._labels: dict[str, str] = {value: label for label, value in self._values.items()}

    def label(self, kind: str, value: str) -> str:
        if value in self._labels:
            return self._labels[value]
        count = sum(1 for label in self._values if label.startswith(f"<<{kind}_"))
        label = f"<<{kind}_{count + 1}>>"
        self._values[label] = value
        self._labels[value] = label
        return label

    def value(self, label: str) -> str | None:
        return self._values.get(label)

    def to_dict(self) -> dict[str, str]:
        return dict(self._values)


class Pseudonymizer:
    def __init__(self, personal_fields: frozenset[str], labels: LabelMap) -> None:
        self._fields = personal_fields
        self.labels = labels

    def mask_text(self, text: str) -> str:
        text = EMAIL.sub(lambda match: self.labels.label("email", match.group()), text)
        return PHONE.sub(lambda match: self.labels.label("phone", match.group()), text)

    def mask_data(self, data: Any) -> Any:
        """Mask a structure from a source: personal fields by name, emails and phones anywhere."""
        if isinstance(data, dict):
            return {
                key: self._mask_field(value) if str(key).lower() in self._fields else self.mask_data(value)
                for key, value in data.items()
            }
        if isinstance(data, list | tuple):
            return [self.mask_data(item) for item in data]
        if isinstance(data, str):
            return self.mask_text(data)
        return data

    def mask_source_text(self, text: str) -> str:
        """A source answers in text that often holds a structure: masked by its fields when it does."""
        stripped = text.strip()
        if stripped[:1] in ("{", "["):
            for parse in (json.loads, _python_repr):
                try:
                    data = parse(stripped)
                except (ValueError, SyntaxError, RecursionError):
                    continue
                return json.dumps(self.mask_data(data), ensure_ascii=False, default=str)
        return self.mask_text(text)

    def _mask_field(self, value: Any) -> Any:
        if value is None or value == "":
            return value
        if isinstance(value, dict | list | tuple):
            return (
                [self._mask_field(item) for item in value]
                if not isinstance(value, dict)
                else {key: self._mask_field(item) for key, item in value.items()}
            )
        return self.labels.label("pd", str(value))

    def unmask(self, data: Any) -> Any:
        """Put the values back into the arguments of a call to a source."""
        if isinstance(data, dict):
            return {key: self.unmask(value) for key, value in data.items()}
        if isinstance(data, list):
            return [self.unmask(item) for item in data]
        if isinstance(data, str):
            return LABEL.sub(lambda match: self.labels.value(match.group()) or match.group(), data)
        return data
