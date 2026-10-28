from internal.privacy.pseudonymizer import LabelMap
from internal.privacy.rich import RichMessageBuilder


def blocks(text, labels=None):
    return list(RichMessageBuilder(labels or LabelMap()).markdown(text).build().blocks)


def test_a_paragraph_with_bold_italic_code_and_a_hidden_email_inside_bold():
    labels = LabelMap()
    label = labels.label("email", "client@example.com")
    assert blocks(f"Клиент **{label}**: _отказ_ модели, код `REAL_PERSON`", labels) == [
        {
            "type": "paragraph",
            "text": [
                "Клиент ",
                {"type": "bold", "text": {"type": "spoiler", "text": "client@example.com"}},
                ": ",
                {"type": "italic", "text": "отказ"},
                " модели, код ",
                {"type": "code", "text": "REAL_PERSON"},
            ],
        }
    ]


def test_a_table_has_header_cells_alignment_and_hidden_values():
    labels = LabelMap()
    label = labels.label("email", "client@example.com")
    table = blocks(f"| Время | Токены |\n|---|--:|\n| 18:16 | {label} |", labels)[0]
    assert table["type"] == "table" and table["is_bordered"] is True
    header, row = table["cells"]
    assert header == [
        {"text": "Время", "align": "left", "is_header": True},
        {"text": "Токены", "align": "right", "is_header": True},
    ]
    assert row == [
        {"text": "18:16", "align": "left"},
        {"text": {"type": "spoiler", "text": "client@example.com"}, "align": "right"},
    ]


def test_headings_lists_quotes_code_and_dividers():
    result = blocks(
        "## Что делать\n\n1. Убрать **Ghostface**\n2. Сменить формат\n\n- один\n\n> цитата\n\n---\n\n```sql\nselect 1\n```"
    )
    assert result[0] == {"type": "heading", "size": 4, "text": "Что делать"}
    assert result[1] == {
        "type": "list",
        "items": [
            {
                "blocks": [{"type": "paragraph", "text": ["Убрать ", {"type": "bold", "text": "Ghostface"}]}],
                "type": "1",
                "value": 1,
            },
            {"blocks": [{"type": "paragraph", "text": "Сменить формат"}], "type": "1", "value": 2},
        ],
    }
    assert result[2] == {"type": "list", "items": [{"blocks": [{"type": "paragraph", "text": "один"}]}]}
    assert result[3] == {"type": "blockquote", "blocks": [{"type": "paragraph", "text": "цитата"}]}
    assert result[4] == {"type": "divider"}
    assert result[5] == {"type": "pre", "text": "select 1", "language": "sql"}


def test_a_label_the_case_does_not_know_and_raw_html_stay_as_written():
    assert blocks("<<email_9>> и <b>жирный</b>") == [{"type": "paragraph", "text": "<<email_9>> и <b>жирный</b>"}]


def test_an_answer_about_a_case_puts_each_part_under_its_heading():
    from internal.agent.answer import CASE_PARTS, Answer
    from internal.words import Words

    data = {"kind": "case", "attachments": [], "text": None, **{part: f"**{part}**" for part in CASE_PARTS}}
    rendered = Answer.from_input(data).render(LabelMap(), Words("en")).blocks
    assert [block["type"] for block in rendered] == ["heading", "paragraph"] * len(CASE_PARTS)
    assert rendered[0] == {"type": "heading", "size": 4, "text": "What the client saw"}
    assert rendered[1] == {"type": "paragraph", "text": {"type": "bold", "text": "client_saw"}}
