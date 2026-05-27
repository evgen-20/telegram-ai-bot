"""Cancel callback and text message handlers — manage CC process via MessageQueue."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, InaccessibleMessage, Message

from telegram_bot.core.messages import t
from telegram_bot.core.services.message_queue import MessageQueue
from telegram_bot.core.services.session_backend import BackendDispatcher, SessionBackend
from telegram_bot.core.services.tmux_manager import TmuxManager
from telegram_bot.core.services.topic_config import TopicConfig
from telegram_bot.core.types import ChannelKey, channel_key

logger = logging.getLogger(__name__)

router = Router(name="cancel")


def _callback_channel_key(message: Message | InaccessibleMessage) -> ChannelKey:
    """Extract ChannelKey from a callback message.

    Callback messages may be InaccessibleMessage (no message_thread_id).
    Fall back to (chat_id, None) if attribute is missing.
    """
    thread_id = getattr(message, "message_thread_id", None)
    return (message.chat.id, thread_id)


def _backend_for(
    dispatcher: BackendDispatcher,
    topic_config: TopicConfig | None,
    channel_key: ChannelKey,
) -> SessionBackend:
    """Resolve the engine-appropriate ``SessionBackend`` for *channel_key*."""
    _, thread_id = channel_key
    if topic_config is not None and thread_id is not None:
        engine = topic_config.get_topic(thread_id).engine
    else:
        engine = "claude"
    return dispatcher.for_engine(engine)


@router.callback_query(F.data == "cancel_cc")
async def handle_cancel_cc(
    callback: CallbackQuery,
    queue: MessageQueue,
    tmux_manager: TmuxManager,
    topic_config: TopicConfig | None = None,
    backend_dispatcher: BackendDispatcher | None = None,
) -> None:
    """Handle cancel button press: interrupt active backend or kill subprocess and clear queue."""
    message = callback.message
    if message is None:
        await callback.answer()
        return

    key = _callback_channel_key(message)
    backend: SessionBackend | TmuxManager = (
        _backend_for(backend_dispatcher, topic_config, key)
        if backend_dispatcher is not None
        else tmux_manager
    )
    tmux_acted = backend.is_active(key)
    if tmux_acted:
        await backend.cancel(key)
    cancelled = await queue.cancel(key)
    acted = cancelled or tmux_acted

    if acted:
        logger.info("User cancelled CC processing for %s", key)
        try:
            await message.edit_reply_markup(reply_markup=None)  # type: ignore[union-attr]
        except Exception:
            logger.debug("Failed to remove cancel buttons", exc_info=True)
        await message.answer(t("ui.cancelled"))
    else:
        logger.debug("Cancel pressed but no active process for %s", key)
        try:
            await message.edit_reply_markup(reply_markup=None)  # type: ignore[union-attr]
        except Exception:
            logger.debug("Failed to remove orphaned cancel buttons", exc_info=True)

    await callback.answer(t("ui.already_finished") if not acted else None)


@router.message(F.text == t("ui.btn_cancel"))
async def handle_cancel_text(
    message: Message,
    queue: MessageQueue,
    tmux_manager: TmuxManager,
    topic_config: TopicConfig | None = None,
    backend_dispatcher: BackendDispatcher | None = None,
) -> None:
    """Handle reply-keyboard cancel: interrupt active backend or kill subprocess + clear queue."""
    key = channel_key(message)
    backend: SessionBackend | TmuxManager = (
        _backend_for(backend_dispatcher, topic_config, key)
        if backend_dispatcher is not None
        else tmux_manager
    )
    tmux_acted = backend.is_active(key)
    if tmux_acted:
        await backend.cancel(key)
    cancelled = await queue.cancel(key)

    if cancelled or tmux_acted:
        logger.info("User cancelled CC processing (text) for %s", key)
        await message.answer(t("ui.cancelled"))
    else:
        logger.debug("Cancel text pressed but no active process for %s", key)
        await message.answer(t("ui.nothing_to_cancel"))
