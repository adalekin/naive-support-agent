import subprocess

import pytest

from internal.sources.attachments import AttachmentReader
from internal.telegram import Attachment, TelegramGateway

BOT = "https://api.telegram.org/bot1234:test-token"
FILES = "https://api.telegram.org/file/bot1234:test-token"


def serve(httpx_mock, file_id, content):
    httpx_mock.add_response(
        url=f"{BOT}/getFile",
        json={"ok": True, "result": {"file_id": file_id, "file_path": f"f/{file_id}", "file_size": len(content)}},
    )
    httpx_mock.add_response(url=f"{FILES}/f/{file_id}", content=content)


@pytest.fixture
def reader():
    return AttachmentReader(TelegramGateway("1234:test-token", None))


async def test_screenshot_goes_to_the_model_as_an_image(httpx_mock, reader):
    serve(httpx_mock, "shot", b"\xff\xd8jpeg")
    read = await reader.read(Attachment("photo", "shot", None, "image/jpeg", 6))
    assert read.unread_reason is None
    assert read.blocks[0]["type"] == "image"


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "v.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=2:size=160x120:rate=10", str(path)],
        check=True,
    )
    return path


async def test_video_is_read_as_frames(httpx_mock, reader, video):
    serve(httpx_mock, "rec", video.read_bytes())
    read = await reader.read(Attachment("video", "rec", "rec.mp4", "video/mp4", None))
    assert [block["type"] for block in read.blocks].count("image") == 8


async def test_unreadable_attachment_is_named(httpx_mock, reader):
    serve(httpx_mock, "voice", b"ogg")
    read = await reader.read(Attachment("voice", "voice", None, "audio/ogg", 3))
    assert read.blocks == [] and "audio/ogg" in read.unread_reason


async def test_attachment_that_cannot_be_downloaded(httpx_mock, reader):
    httpx_mock.add_response(url=f"{BOT}/getFile", status_code=400, json={"ok": False, "description": "file is too big"})
    read = await reader.read(Attachment("document", "big", "dump.zip", "application/zip", 10**9))
    assert read.unread_reason == "the file could not be downloaded"
