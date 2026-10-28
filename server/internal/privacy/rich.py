"""What people read in a rich message: the model's Markdown as blocks, the real values under a spoiler.

The model writes Markdown with GFM tables; the blocks are those of ``sendRichMessage`` (Bot API 10.1,
core.telegram.org/bots/api#inputrichblock). Labels are put back here, never inside the Markdown,
so what is hidden is decided by the assistant and not by escaping.
"""

import re
from typing import Any

from markdown_it import MarkdownIt
from markdown_it.token import Token

from internal.privacy.pseudonymizer import LABEL, LabelMap
from internal.telegram import RichMessage

#: CommonMark with tables and strikethrough; raw HTML of the model is text, not markup.
PARSER = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
#: A Markdown heading of level N is shown at size N + 2: the largest sizes are for pages, not chat.
HEADING_SIZE_SHIFT = 2
INLINE_STYLES = {"strong": "bold", "em": "italic", "s": "strikethrough"}
ALIGN = re.compile(r"text-align:(left|center|right)")

RichText = str | dict[str, Any] | list[Any]


class RichMessageBuilder:
    """Builds the blocks of one rich message."""

    def __init__(self, labels: LabelMap) -> None:
        self._labels = labels
        self._blocks: list[dict[str, Any]] = []

    def heading(self, text: str, size: int = 4) -> "RichMessageBuilder":
        self._blocks.append({"type": "heading", "size": size, "text": text})
        return self

    def mentions(self, people: list[tuple[int, str]]) -> "RichMessageBuilder":
        """A line that calls each person by their Telegram number: it works without a user name."""
        text: list[RichText] = []
        for index, (user_id, name) in enumerate(people):
            if index:
                text.append(", ")
            text.append(
                {"type": "text_mention", "text": name, "user": {"id": user_id, "is_bot": False, "first_name": name}}
            )
        self._blocks.append({"type": "paragraph", "text": text})
        return self

    def paragraph(self, text: str) -> "RichMessageBuilder":
        self._blocks.append({"type": "paragraph", "text": self._revealed(text)})
        return self

    def markdown(self, text: str) -> "RichMessageBuilder":
        tokens = PARSER.parse(text)
        blocks, _ = self._blocks_until(tokens, 0, None)
        self._blocks.extend(blocks)
        return self

    def build(self) -> RichMessage:
        return RichMessage(tuple(self._blocks))

    def _blocks_until(self, tokens: list[Token], index: int, closing: str | None) -> tuple[list[dict[str, Any]], int]:
        blocks: list[dict[str, Any]] = []
        while index < len(tokens):
            token = tokens[index]
            if closing is not None and token.type == closing:
                return blocks, index + 1
            if token.type == "heading_open":
                size = min(6, int(token.tag[1]) + HEADING_SIZE_SHIFT)
                blocks.append({"type": "heading", "size": size, "text": self._inline(tokens[index + 1])})
                index += 3
            elif token.type == "paragraph_open":
                blocks.append({"type": "paragraph", "text": self._inline(tokens[index + 1])})
                index += 3
            elif token.type in ("bullet_list_open", "ordered_list_open"):
                ordered = token.type == "ordered_list_open"
                start = int(token.attrGet("start") or 1)
                items, index = self._list_items(tokens, index + 1, token.type.replace("_open", "_close"))
                if ordered:
                    items = [{**item, "type": "1", "value": start + number} for number, item in enumerate(items)]
                blocks.append({"type": "list", "items": items})
            elif token.type == "blockquote_open":
                inner, index = self._blocks_until(tokens, index + 1, "blockquote_close")
                blocks.append({"type": "blockquote", "blocks": inner})
            elif token.type == "table_open":
                rows, index = self._table_rows(tokens, index + 1)
                blocks.append({"type": "table", "cells": rows, "is_bordered": True})
            elif token.type in ("fence", "code_block"):
                block: dict[str, Any] = {"type": "pre", "text": self._revealed(token.content.rstrip("\n"))}
                if token.info.strip():
                    block["language"] = token.info.strip().split()[0]
                blocks.append(block)
                index += 1
            elif token.type == "hr":
                blocks.append({"type": "divider"})
                index += 1
            else:
                index += 1
        return blocks, index

    def _list_items(self, tokens: list[Token], index: int, closing: str) -> tuple[list[dict[str, Any]], int]:
        items = []
        while tokens[index].type != closing:
            if tokens[index].type == "list_item_open":
                inner, index = self._blocks_until(tokens, index + 1, "list_item_close")
                items.append({"blocks": inner})
            else:
                index += 1
        return items, index + 1

    def _table_rows(self, tokens: list[Token], index: int) -> tuple[list[list[dict[str, Any]]], int]:
        rows: list[list[dict[str, Any]]] = []
        while tokens[index].type != "table_close":
            token = tokens[index]
            if token.type == "tr_open":
                rows.append([])
            elif token.type in ("th_open", "td_open"):
                match = ALIGN.search(str(token.attrGet("style") or ""))
                cell: dict[str, Any] = {
                    "text": self._inline(tokens[index + 1]),
                    "align": match.group(1) if match else "left",
                }
                if token.type == "th_open":
                    cell["is_header"] = True
                rows[-1].append(cell)
                index += 2
            index += 1
        return rows, index + 1

    def _inline(self, token: Token) -> RichText:
        stack: list[tuple[str | None, list[RichText]]] = [(None, [])]
        for child in token.children or []:
            if child.type == "text":
                stack[-1][1].extend(self._pieces(child.content))
            elif child.type == "code_inline":
                stack[-1][1].append({"type": "code", "text": self._joined(self._pieces(child.content))})
            elif child.type in ("softbreak", "hardbreak"):
                stack[-1][1].append("\n")
            elif child.type.endswith("_open") and child.type[:-5] in INLINE_STYLES:
                stack.append((INLINE_STYLES[child.type[:-5]], []))
            elif child.type == "link_open":
                stack.append((f"url:{child.attrGet('href')}", []))
            elif child.type.endswith("_close") and len(stack) > 1:
                style, content = stack.pop()
                assert style is not None
                if style.startswith("url:"):
                    stack[-1][1].append({"type": "url", "text": self._joined(content), "url": style[4:]})
                else:
                    stack[-1][1].append({"type": style, "text": self._joined(content)})
            elif child.type == "image":
                stack[-1][1].extend(self._pieces(child.content))
        while len(stack) > 1:
            _, content = stack.pop()
            stack[-1][1].extend(content)
        return self._joined(stack[0][1])

    def _revealed(self, text: str) -> RichText:
        return self._joined(self._pieces(text))

    def _pieces(self, text: str) -> list[RichText]:
        pieces: list[RichText] = []
        position = 0
        for match in LABEL.finditer(text):
            value = self._labels.value(match.group())
            if value is None:
                continue
            if match.start() > position:
                pieces.append(text[position : match.start()])
            pieces.append({"type": "spoiler", "text": value})
            position = match.end()
        if position < len(text):
            pieces.append(text[position:])
        return pieces

    @staticmethod
    def _joined(pieces: list[RichText]) -> RichText:
        merged: list[RichText] = []
        for piece in pieces:
            if isinstance(piece, str) and merged and isinstance(merged[-1], str):
                merged[-1] += piece
            else:
                merged.append(piece)
        if not merged:
            return ""
        return merged[0] if len(merged) == 1 else merged
