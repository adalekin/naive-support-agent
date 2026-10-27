"""The files of a request: what the client showed on a screenshot or a screen recording.

They go to the model as they are, as images, documents and text: personal data in them is
not recognised (``case-investigation``, the client's files reach the model as they are).
"""

import asyncio
import base64
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from internal.telegram import Attachment, TelegramGateway, TelegramUnavailable

#: How many frames of a video the model looks at, spread over its length.
VIDEO_FRAMES = 8
FFMPEG_TIMEOUT_SECONDS = 120
IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
VIDEO_KINDS = {"video", "animation", "video_note"}


@dataclass(frozen=True)
class ReadAttachment:
    """An attachment as the model gets it, and how to show it again to people."""

    attachment: Attachment
    #: Content blocks for the model; empty when the file could not be read.
    blocks: list[dict[str, Any]]
    #: Why it could not be read, in plain words.
    unread_reason: str | None

    @property
    def display_name(self) -> str:
        return self.attachment.name or self.attachment.kind


class AttachmentReader:
    def __init__(self, telegram: TelegramGateway) -> None:
        self._telegram = telegram

    async def read(self, attachment: Attachment) -> ReadAttachment:
        try:
            content = await self._telegram.download(attachment.file_id)
        except TelegramUnavailable:
            return ReadAttachment(attachment, [], "the file could not be downloaded")

        mime = attachment.mime_type or ""
        if attachment.kind == "photo" or mime in IMAGE_TYPES:
            return ReadAttachment(attachment, [_image(mime or "image/jpeg", content)], None)
        if mime == "application/pdf":
            return ReadAttachment(
                attachment,
                [
                    {
                        "type": "document",
                        "source": {"type": "base64", "media_type": "application/pdf", "data": _b64(content)},
                    }
                ],
                None,
            )
        if mime.startswith("text/") or mime in ("application/json", "application/xml"):
            text = content.decode("utf-8", errors="replace")
            return ReadAttachment(
                attachment, [{"type": "text", "text": f"File {attachment.name or ''}:\n{text}"}], None
            )
        if attachment.kind in VIDEO_KINDS or mime.startswith("video/"):
            frames = await _frames(content)
            if not frames:
                return ReadAttachment(attachment, [], "the frames of the video could not be taken")
            note = {"type": "text", "text": f"{len(frames)} frames of the video, in order:"}
            return ReadAttachment(attachment, [note, *(_image("image/jpeg", frame) for frame in frames)], None)
        return ReadAttachment(attachment, [], f"files of the type {mime or attachment.kind} are not read")


def _b64(content: bytes) -> str:
    return base64.standard_b64encode(content).decode()


def _image(media_type: str, content: bytes) -> dict[str, Any]:
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": _b64(content)}}


async def _run(*command: str) -> bytes | None:
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    try:
        out, _ = await asyncio.wait_for(process.communicate(), FFMPEG_TIMEOUT_SECONDS)
    except TimeoutError:
        process.kill()
        return None
    return out if process.returncode == 0 else None


async def _frames(content: bytes) -> list[bytes]:
    with tempfile.TemporaryDirectory() as directory:
        video = Path(directory) / "video"
        video.write_bytes(content)
        probed = await _run(
            "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(video)
        )
        try:
            duration = float(probed or b"")
        except ValueError:
            logger.warning("A video of a request has no readable duration")
            return []
        frames = []
        for index in range(VIDEO_FRAMES):
            moment = duration * (index + 0.5) / VIDEO_FRAMES
            frame = await _run(
                "ffmpeg", "-v", "error", "-ss", f"{moment:.2f}", "-i", str(video), "-frames:v", "1",
                "-vf", "scale='min(1280,iw)':-2", "-f", "image2", "-c:v", "mjpeg", "pipe:1",
            )  # fmt: skip
            if frame:
                frames.append(frame)
        return frames
