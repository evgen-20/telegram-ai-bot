"""Audio file handler — transcribes meeting recordings with speaker diarization.

Handles standalone audio messages (F.audio) and documents with an audio/*
mime type. Downloads the file, runs Deepgram in diarized mode, formats the
result as a Markdown transcript, and sends it back as a document.
"""

from __future__ import annotations

import contextlib
import logging
import time
from pathlib import Path
from typing import Any

from aiogram import Bot, F, Router
from aiogram.types import FSInputFile, Message

from telegram_bot.core.services.claude import SessionManager
from telegram_bot.core.services.transcriber import Transcriber, TranscriptionError
from telegram_bot.core.utils.fs import sanitize_filename

logger = logging.getLogger(__name__)

router = Router(name="audio")

# Bot API can only download files up to 20 MB via getFile. Files larger
# than this must be delivered through a different channel (scp, direct URL).
_MAX_AUDIO_SIZE = 20 * 1024 * 1024
_MAX_AUDIO_SIZE_MB = 20


def _format_timestamp(seconds: float) -> str:
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _segments_to_markdown(segments: list[dict[str, Any]], source_name: str) -> str:
    lines = [f"# Транскрипт: {source_name}", ""]
    if not segments:
        lines.append("_Пусто (тишина или неразборчиво)._")
        return "\n".join(lines) + "\n"
    for seg in segments:
        ts = _format_timestamp(seg["start"])
        speaker = seg.get("speaker", 0)
        text = seg["text"].strip()
        lines.append(f"[{ts}] **Speaker {speaker}:** {text}")
        lines.append("")
    return "\n".join(lines)


async def _transcribe_and_reply(
    message: Message,
    bot: Bot,
    session_manager: SessionManager,
    transcriber: Transcriber,
    *,
    file_id: str,
    file_unique_id: str,
    original_name: str,
    file_size: int | None,
) -> None:
    logger.info(
        "Audio for transcription from %s: %s (%s bytes)",
        message.from_user and message.from_user.id,
        original_name,
        file_size,
    )

    if file_size is not None and file_size > _MAX_AUDIO_SIZE:
        await message.answer(
            f"Файл больше {_MAX_AUDIO_SIZE_MB} МБ — Bot API не сможет его скачать. "
            "Сожми в mp3 32-64 kbps или закинь на сервер через scp."
        )
        return

    status = await message.answer("⏳ Транскрибирую встречу, это может занять пару минут…")

    tmp_dir = Path(session_manager.file_cache_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    timestamp = int(time.time())
    safe_name = sanitize_filename(original_name)
    src_path = tmp_dir / f"{timestamp}_{file_unique_id}_{safe_name}"

    try:
        await bot.download(file_id, destination=src_path)
    except Exception:
        logger.warning("Failed to download audio file", exc_info=True)
        await status.edit_text("Не удалось скачать файл. Попробуй ещё раз.")
        return

    try:
        audio_bytes = src_path.read_bytes()
        segments = await transcriber.transcribe_meeting(audio_bytes)
    except TranscriptionError as exc:
        logger.warning("Meeting transcription failed: %s", exc)
        await status.edit_text(f"Ошибка транскрипции: {exc}")
        return

    md_text = _segments_to_markdown(segments, original_name)
    stem = safe_name.rsplit(".", 1)[0] or "transcript"
    md_path = tmp_dir / f"{timestamp}_{file_unique_id}_{stem}.md"
    md_path.write_text(md_text, encoding="utf-8")

    try:
        speakers = {seg.get("speaker", 0) for seg in segments}
        caption = (
            f"Транскрипт «{original_name}» · сегментов: {len(segments)} · спикеров: {len(speakers)}"
            if segments
            else f"Транскрипт «{original_name}» пуст (тишина или неразборчиво)"
        )
        await message.reply_document(
            FSInputFile(str(md_path), filename=md_path.name),
            caption=caption,
        )
    except Exception:
        logger.warning("Failed to send transcript document", exc_info=True)
        await status.edit_text("Транскрипт готов, но не удалось отправить файл.")
        return

    with contextlib.suppress(Exception):
        await status.delete()


@router.message(F.audio)
async def handle_audio(
    message: Message,
    bot: Bot,
    session_manager: SessionManager,
    transcriber: Transcriber,
) -> None:
    if not message.audio:
        return
    audio = message.audio
    original_name = audio.file_name or f"audio_{audio.file_unique_id}.mp3"
    await _transcribe_and_reply(
        message,
        bot,
        session_manager,
        transcriber,
        file_id=audio.file_id,
        file_unique_id=audio.file_unique_id,
        original_name=original_name,
        file_size=audio.file_size,
    )


@router.message(F.document.mime_type.startswith("audio/"))
async def handle_audio_document(
    message: Message,
    bot: Bot,
    session_manager: SessionManager,
    transcriber: Transcriber,
) -> None:
    if not message.document:
        return
    doc = message.document
    original_name = doc.file_name or f"audio_{doc.file_unique_id}"
    await _transcribe_and_reply(
        message,
        bot,
        session_manager,
        transcriber,
        file_id=doc.file_id,
        file_unique_id=doc.file_unique_id,
        original_name=original_name,
        file_size=doc.file_size,
    )
