import json

import httpx
import pytest

from internal.telegram import (
    Button,
    ChatClosed,
    Entity,
    MembershipChanged,
    Pressed,
    Said,
    TelegramGateway,
    TelegramUnavailable,
    Text,
    parse_event,
    utf16_length,
)

BOT = "https://api.telegram.org/bot1234:test-token"


def ok(result):
    return {"ok": True, "result": result}


async def test_get_updates_asks_only_for_the_kinds_the_assistant_handles(httpx_mock):
    httpx_mock.add_response(url=f"{BOT}/getUpdates", json=ok([]))
    gateway = TelegramGateway("1234:test-token", None)
    assert await gateway.get_updates(offset=7, wait_seconds=1) == []
    sent = json.loads(httpx_mock.get_request().content)
    assert sent == {"timeout": 1, "allowed_updates": ["message", "my_chat_member", "callback_query"], "offset": 7}


async def test_message_goes_with_entities_reply_topic_and_buttons(httpx_mock):
    httpx_mock.add_response(url=f"{BOT}/sendMessage", json=ok({"message_id": 55}))
    gateway = TelegramGateway("1234:test-token", None)
    number = await gateway.send_message(
        -1001,
        Text("mail: a@b.c", (Entity("spoiler", 6, 5),)),
        reply_to=10,
        thread_id=3,
        keyboard=[[Button("ok", "f:1:up")]],
    )
    assert number == [55]
    sent = json.loads(httpx_mock.get_request().content)
    assert sent["entities"] == [{"type": "spoiler", "offset": 6, "length": 5}]
    assert sent["reply_parameters"] == {"message_id": 10, "allow_sending_without_reply": True}
    assert sent["message_thread_id"] == 3
    assert sent["reply_markup"] == {"inline_keyboard": [[{"text": "ok", "callback_data": "f:1:up"}]]}


async def test_photo_goes_under_a_spoiler(httpx_mock):
    httpx_mock.add_response(url=f"{BOT}/sendPhoto", json=ok({"message_id": 9}))
    gateway = TelegramGateway("1234:test-token", None)
    await gateway.send_media(5, "photo", b"\x89PNG", "shot.png", reply_to=1)
    body = httpx_mock.get_request().content
    assert b'name="has_spoiler"\r\n\r\ntrue' in body
    assert b'filename="shot.png"' in body


async def test_blocked_chat_is_told_apart_from_a_failure(httpx_mock):
    httpx_mock.add_response(
        url=f"{BOT}/sendMessage", status_code=403, json={"ok": False, "description": "Forbidden: bot was blocked"}
    )
    httpx_mock.add_response(url=f"{BOT}/sendMessage", status_code=502, json={"ok": False, "description": "Bad"})
    gateway = TelegramGateway("1234:test-token", None)
    with pytest.raises(ChatClosed):
        await gateway.send_message(5, Text("x"))
    with pytest.raises(TelegramUnavailable):
        await gateway.send_message(5, Text("x"))


async def test_unreachable_telegram_does_not_log_the_token(httpx_mock, capfd):
    httpx_mock.add_exception(httpx.ConnectError("boom"))
    gateway = TelegramGateway("1234:test-token", None)
    with pytest.raises(TelegramUnavailable) as error:
        await gateway.leave_chat(5)
    assert "test-token" not in str(error.value)


async def test_download_reads_the_file(httpx_mock):
    httpx_mock.add_response(
        url=f"{BOT}/getFile", json=ok({"file_id": "f", "file_path": "photos/1.jpg", "file_size": 3})
    )
    httpx_mock.add_response(url="https://api.telegram.org/file/bot1234:test-token/photos/1.jpg", content=b"abc")
    assert await TelegramGateway("1234:test-token", None).download("f") == b"abc"


# Samples shaped as in the Bot API reference (core.telegram.org/bots/api, Update and Message).


def test_group_message_with_mention_reply_and_photo():
    event = parse_event(
        {
            "update_id": 100,
            "message": {
                "message_id": 20,
                "from": {"id": 777, "is_bot": False, "first_name": "Anna", "username": "anna_s"},
                "chat": {"id": -1001, "type": "supergroup", "title": "Support"},
                "date": 1,
                "text": "@Assistant_Test_Bot глянь 🙂",
                "entities": [{"type": "mention", "offset": 0, "length": 19}],
                "reply_to_message": {
                    "message_id": 19,
                    "from": {"id": 900, "is_bot": False, "first_name": "Client"},
                    "chat": {"id": -1001, "type": "supergroup"},
                    "date": 1,
                    "caption": "не работает",
                    "photo": [
                        {"file_id": "small", "file_unique_id": "s", "width": 90, "height": 90},
                        {"file_id": "large", "file_unique_id": "l", "width": 900, "height": 900},
                    ],
                },
            },
        }
    )
    assert isinstance(event, Said)
    assert event.sender_id == 777
    assert event.mentioned_usernames == {"assistant_test_bot"}
    assert event.reply_to.text == "не работает"
    assert [a.file_id for a in event.reply_to.attachments] == ["large"]
    assert not event.on_behalf_of_chat


def test_mention_after_an_emoji_is_read_in_utf16_offsets():
    text = "🙂 @owner_name ok"
    event = parse_event(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "from": {"id": 1, "is_bot": False, "first_name": "A"},
                "chat": {"id": -1001, "type": "group"},
                "date": 1,
                "text": text,
                "entities": [{"type": "mention", "offset": 3, "length": 11}],
            },
        }
    )
    assert event.mentioned_usernames == {"owner_name"}
    assert utf16_length("🙂") == 2


def test_anonymous_administrator_is_marked():
    event = parse_event(
        {
            "update_id": 2,
            "message": {
                "message_id": 3,
                "from": {"id": 1087968824, "is_bot": True, "first_name": "Group", "username": "GroupAnonymousBot"},
                "sender_chat": {"id": -1001, "type": "supergroup", "title": "Support"},
                "chat": {"id": -1001, "type": "supergroup"},
                "date": 1,
                "text": "@assistant_test_bot hi",
            },
        }
    )
    assert event.on_behalf_of_chat


def test_forwarded_message_is_marked_and_keeps_the_forwarder_as_sender():
    event = parse_event(
        {
            "update_id": 3,
            "message": {
                "message_id": 4,
                "from": {"id": 777, "is_bot": False, "first_name": "Anna"},
                "chat": {"id": 777, "type": "private"},
                "date": 1,
                "forward_origin": {"type": "user", "date": 1, "sender_user": {"id": 900, "is_bot": False}},
                "text": "деньги списались, а результата нет",
            },
        }
    )
    assert event.forwarded and event.sender_id == 777


def test_button_press_and_membership():
    pressed = parse_event(
        {
            "update_id": 4,
            "callback_query": {
                "id": "q1",
                "from": {"id": 777, "is_bot": False, "first_name": "Anna"},
                "message": {"message_id": 30, "chat": {"id": -1001, "type": "group"}, "date": 1},
                "data": "f:1:up",
            },
        }
    )
    assert pressed == Pressed(4, -1001, 30, 777, "q1", "f:1:up")
    joined = parse_event(
        {
            "update_id": 5,
            "my_chat_member": {
                "chat": {"id": -5, "type": "group"},
                "from": {"id": 1, "is_bot": False, "first_name": "X"},
                "date": 1,
                "old_chat_member": {"status": "left", "user": {"id": 2, "is_bot": True, "first_name": "B"}},
                "new_chat_member": {"status": "member", "user": {"id": 2, "is_bot": True, "first_name": "B"}},
            },
        }
    )
    assert joined == MembershipChanged(5, -5, "group", "member")


def test_long_text_is_cut_at_line_breaks_and_keeps_every_spoiler():
    from internal.telegram import split_text

    line = "x" * 99 + "\n"
    body = line * 60
    secret_at = utf16_length(body) - 3
    text = Text(body + "client@example.com end", (Entity("spoiler", secret_at, 21),))
    parts = split_text(text, 4096)
    assert len(parts) == 2
    assert "".join(part.text for part in parts) == text.text
    assert all(utf16_length(part.text) <= 4096 for part in parts)
    assert parts[0].text.endswith("\n")
    hidden = "".join(
        part.text.encode("utf-16-le")[e.offset * 2 : (e.offset + e.length) * 2].decode("utf-16-le")
        for part in parts
        for e in part.entities
    )
    assert hidden == text.text.encode("utf-16-le")[secret_at * 2 : (secret_at + 21) * 2].decode("utf-16-le")


async def test_long_answer_goes_as_several_messages_with_buttons_under_the_last(httpx_mock):
    for number in (1, 2):
        httpx_mock.add_response(url=f"{BOT}/sendMessage", json=ok({"message_id": number}))
    gateway = TelegramGateway("1234:test-token", None)
    numbers = await gateway.send_message(5, Text("a" * 6000), keyboard=[[Button("ok", "f:1:up")]])
    assert numbers == [1, 2]
    first, second = (json.loads(request.content) for request in httpx_mock.get_requests())
    assert "reply_markup" not in first and "reply_markup" in second


async def test_rich_message_goes_with_reply_topic_buttons_and_no_entity_detection(httpx_mock):
    from internal.telegram import RichMessage

    httpx_mock.add_response(url=f"{BOT}/sendRichMessage", json=ok({"message_id": 56}))
    gateway = TelegramGateway("1234:test-token", None)
    blocks = ({"type": "paragraph", "text": [{"type": "spoiler", "text": "a@b.c"}]},)
    number = await gateway.send_rich(
        -1001, RichMessage(blocks), reply_to=10, thread_id=3, keyboard=[[Button("ok", "f:1:up")]]
    )
    assert number == 56
    sent = json.loads(httpx_mock.get_request().content)
    assert sent["rich_message"] == {"blocks": list(blocks), "skip_entity_detection": True}
    assert sent["reply_parameters"] == {"message_id": 10, "allow_sending_without_reply": True}
    assert sent["message_thread_id"] == 3
    assert sent["reply_markup"] == {"inline_keyboard": [[{"text": "ok", "callback_data": "f:1:up"}]]}
