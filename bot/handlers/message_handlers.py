"""
Message moderation pipeline: filters, spam protection, AI analysis, welcomes, and Captcha verification.
Features bounded LRU chat histories, concurrency-safe CAPTCHA verification, and stage-level fault isolation.
"""

import asyncio
import logging
import threading
import time
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple

from aiogram import Bot, F, Router, types
from aiogram.enums import ChatMemberStatus
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.filters.permissions import is_user_chat_admin
from config.settings import settings
from database.models import (
    GroupMemberRepository,
    GroupRepository,
    UserRepository,
)
from moderation.actions import ModerationActions, auto_delete_message, task_manager
from services.ai_service import ai_service
from services.filter_service import filter_service
from services.spam_service import spam_service
from utils.telemetry import telemetry

logger = logging.getLogger("argus.handlers.messages")
router = Router()

# Bounded LRU sliding-window history for AI context per chat
# Format: {chat_id: [username: message_text]}
_chat_histories: OrderedDict[int, List[str]] = OrderedDict()
_history_lock = threading.RLock()
MAX_HISTORY = 10
MAX_TRACKED_CHAT_HISTORIES = 500

# Pending CAPTCHA verifications: { (chat_id, user_id): (captcha_msg_id, join_timestamp) }
_pending_captchas: Dict[Tuple[int, int], Tuple[int, float]] = {}
_captcha_lock = asyncio.Lock()


def format_welcome_or_goodbye(
    template: str, user: types.User, chat_title: Optional[str]
) -> str:
    """Render welcome/goodbye templates supporting both [var] and {var} placeholders."""
    first_name = user.first_name or "Member"
    username = f"@{user.username}" if user.username else first_name
    group_title = chat_title or "this group"

    out = template
    for key, val in (
        ("[first_name]", first_name),
        ("{first_name}", first_name),
        ("[username]", username),
        ("{username}", username),
        ("[group_title]", group_title),
        ("{group_title}", group_title),
    ):
        out = out.replace(key, val)
    return out


def get_recent_history(chat_id: int) -> str:
    """Format recent messages in chat for AI context."""
    with _history_lock:
        history = _chat_histories.get(chat_id, [])
        if not history:
            return "No recent messages."
        return "\n".join(history)


def append_history(chat_id: int, formatted_msg: str) -> None:
    """Append a message to the bounded AI context sliding window."""
    with _history_lock:
        if chat_id in _chat_histories:
            _chat_histories.move_to_end(chat_id)
        else:
            _chat_histories[chat_id] = []
        _chat_histories[chat_id].append(formatted_msg)
        if len(_chat_histories[chat_id]) > MAX_HISTORY:
            _chat_histories[chat_id].pop(0)
        while len(_chat_histories) > MAX_TRACKED_CHAT_HISTORIES:
            _chat_histories.popitem(last=False)


# ---------------------------------------------------------------------------
# Welcomes & CAPTCHA Verification
# ---------------------------------------------------------------------------

@router.message(F.new_chat_members)
async def on_user_join(message: types.Message, bot: Bot) -> None:
    """Handle new members joining. Trigger mute, CAPTCHA challenge, and welcome message."""
    chat_id = message.chat.id
    group_settings = await GroupRepository.get_group(chat_id)
    captcha_enabled = bool(group_settings.get("welcome_captcha", 0))
    anti_raid = bool(group_settings.get("anti_raid", 0))

    if spam_service.record_join(chat_id, anti_raid):
        try:
            await message.answer(
                "🚨 **Raid Warning**: High joining rate detected! Activating mandatory CAPTCHA lockdown."
            )
        except Exception as exc:
            logger.warning("Failed to send raid alert in chat %d: %s", chat_id, exc)
        captcha_enabled = True

    for member in message.new_chat_members or []:
        if member.is_bot:
            try:
                await bot.ban_chat_member(chat_id, member.id)
                await bot.unban_chat_member(chat_id, member.id)
            except Exception as exc:
                logger.warning("Failed to kick joining bot %d in chat %d: %s", member.id, chat_id, exc)
            continue

        await UserRepository.get_or_create_user(
            member.id, member.username, member.first_name, member.last_name
        )
        await GroupMemberRepository.set_member_role(chat_id, member.id, "member")

        if captcha_enabled:
            mute_perms = types.ChatPermissions(
                can_send_messages=False,
                can_send_media_messages=False,
                can_send_polls=False,
                can_send_other_messages=False,
                can_add_web_page_previews=False,
            )
            try:
                await bot.restrict_chat_member(chat_id, member.id, permissions=mute_perms)
            except Exception as exc:
                logger.error("Failed to mute user on join: %s", exc)

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="✅ Verify - I am a Human!",
                            callback_data=f"captcha_verify:{member.id}",
                        )
                    ]
                ]
            )

            timeout_sec = settings.CAPTCHA_TIMEOUT_SECONDS
            try:
                captcha_msg = await message.answer(
                    f"🛡️ **Welcome, {member.first_name}!**\n"
                    f"To prevent spam bots, please click the button below within {timeout_sec} seconds to unmute yourself.",
                    reply_markup=keyboard,
                )
            except Exception as exc:
                logger.error("Failed to send CAPTCHA challenge in chat %d: %s", chat_id, exc)
                continue

            key = (chat_id, member.id)
            async with _captcha_lock:
                _pending_captchas[key] = (captcha_msg.message_id, time.time())
                telemetry.set_gauge("pending_captchas", len(_pending_captchas))

            task_manager.create_task(
                verify_captcha_timeout(chat_id, member, captcha_msg.message_id, bot, timeout_sec),
                name=f"captcha_timeout_{chat_id}_{member.id}",
            )
        else:
            welcome_template = group_settings.get("welcome_message", "Welcome to the group!")
            welcome_text = format_welcome_or_goodbye(
                welcome_template, member, message.chat.title
            )
            try:
                await message.answer(welcome_text)
            except Exception as exc:
                logger.warning("Failed to send welcome message: %s", exc)


async def verify_captcha_timeout(
    chat_id: int,
    user: types.User,
    captcha_msg_id: int,
    bot: Bot,
    timeout_seconds: int = 90,
) -> None:
    """Wait timeout_seconds. If user is still in _pending_captchas, kick them safely."""
    await asyncio.sleep(timeout_seconds)
    key = (chat_id, user.id)
    should_kick = False
    async with _captcha_lock:
        if key in _pending_captchas and _pending_captchas[key][0] == captcha_msg_id:
            _pending_captchas.pop(key, None)
            telemetry.set_gauge("pending_captchas", len(_pending_captchas))
            should_kick = True

    if not should_kick:
        return

    logger.info("CAPTCHA timeout for user %d in chat %d. Kicking...", user.id, chat_id)
    telemetry.increment("captcha_timeouts_total")
    try:
        await bot.delete_message(chat_id, captcha_msg_id)
    except Exception:
        pass
    try:
        await bot.ban_chat_member(chat_id, user.id)
        await bot.unban_chat_member(chat_id, user.id)
        await bot.send_message(
            chat_id,
            f"🚫 **{user.first_name}** failed CAPTCHA verification and was kicked.",
        )
    except Exception as exc:
        logger.error("Failed to kick user on CAPTCHA timeout: %s", exc)


@router.callback_query(F.data.startswith("captcha_verify:"))
async def on_captcha_click(callback: types.CallbackQuery, bot: Bot) -> None:
    """Handle verification click atomically."""
    try:
        user_id = int((callback.data or "").split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("⚠️ Invalid verification data.", show_alert=True)
        return

    clicker = callback.from_user
    if not callback.message or not callback.message.chat:
        return
    chat_id = callback.message.chat.id

    if clicker.id != user_id:
        await callback.answer("❌ This verification prompt is not for you!", show_alert=True)
        return

    key = (chat_id, user_id)
    async with _captcha_lock:
        pending = _pending_captchas.pop(key, None)
        telemetry.set_gauge("pending_captchas", len(_pending_captchas))

    if pending is not None:
        unmute_perms = types.ChatPermissions(
            can_send_messages=True,
            can_send_audios=True,
            can_send_documents=True,
            can_send_photos=True,
            can_send_videos=True,
            can_send_video_notes=True,
            can_send_voice_notes=True,
            can_send_polls=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True,
            can_invite_users=True,
        )
        try:
            await bot.restrict_chat_member(chat_id, user_id, permissions=unmute_perms)
            await callback.answer("✅ Verified successfully! Welcome!", show_alert=True)
            await callback.message.delete()
            telemetry.increment("captcha_verified_total")
        except Exception as exc:
            logger.error("Failed to restore permissions on CAPTCHA pass: %s", exc)
            await callback.answer("❌ Failed to verify. Contact admins.", show_alert=True)
            return

        group_settings = await GroupRepository.get_group(chat_id)
        welcome_template = group_settings.get("welcome_message", "Welcome to the group!")
        welcome_text = format_welcome_or_goodbye(
            welcome_template, clicker, callback.message.chat.title
        )
        try:
            await bot.send_message(chat_id, welcome_text)
        except Exception as exc:
            logger.warning("Failed to send post-captcha welcome message: %s", exc)
    else:
        await callback.answer("⚠️ Your captcha session has expired or is invalid.", show_alert=True)


@router.message(F.left_chat_member)
async def on_user_leave(message: types.Message) -> None:
    """Handle users leaving/being kicked. Send goodbye message."""
    chat_id = message.chat.id
    member = message.left_chat_member
    if not member:
        return

    actor_id = message.from_user.id if message.from_user else member.id
    new_role = "banned" if actor_id != member.id else "member"
    await GroupMemberRepository.set_member_role(chat_id, member.id, new_role)

    group_settings = await GroupRepository.get_group(chat_id)
    goodbye_template = group_settings.get("goodbye_message", "Goodbye!")
    goodbye_text = format_welcome_or_goodbye(
        goodbye_template, member, message.chat.title
    )
    try:
        await message.answer(goodbye_text)
    except Exception as exc:
        logger.warning("Failed to send goodbye message: %s", exc)


# ---------------------------------------------------------------------------
# Message Moderation Pipeline
# ---------------------------------------------------------------------------

@router.message()
async def on_message(message: types.Message, bot: Bot) -> None:
    """Monitor and process every incoming text message or attachment."""
    chat_id = message.chat.id
    user = message.from_user

    # 1. Scope restriction (ignore other chats if config limits it)
    if settings.GROUP_CHAT_ID != 0 and chat_id != settings.GROUP_CHAT_ID:
        return

    # Skip bot commands, let command routers catch them
    if message.text and message.text.startswith("/"):
        return

    if not user:
        return

    await UserRepository.get_or_create_user(
        user.id, user.username, user.first_name, user.last_name
    )

    is_admin = await is_user_chat_admin(bot, chat_id, user.id)
    text = (message.text or message.caption or "").strip()

    if not is_admin:
        # A. Run Banned Words Filters
        try:
            filter_result = await filter_service.check_message(chat_id, text)
        except Exception as exc:
            logger.error("FilterService failed on chat %d: %s", chat_id, exc, exc_info=True)
            filter_result = None

        if filter_result:
            action = filter_result["action"]
            reason = filter_result["reason"]
            logger.info(
                "Filter triggered in chat %d on user %d: action=%s reason=%s",
                chat_id,
                user.id,
                action,
                reason,
            )

            try:
                await message.delete()
            except Exception as exc:
                logger.warning("Could not delete filtered message: %s", exc)

            if action == "warn":
                await ModerationActions.warn(chat_id, user, reason, 0, bot)
            elif action == "mute":
                await ModerationActions.mute(chat_id, user, 3600, reason, 0, bot)
            elif action == "ban":
                await ModerationActions.ban(chat_id, user, None, reason, 0, bot)
            return

        # B. Run Spam Protection checks
        try:
            spam_reason = await spam_service.check_spam(chat_id, user.id, message)
        except Exception as exc:
            logger.error("SpamService failed on chat %d: %s", chat_id, exc, exc_info=True)
            spam_reason = None

        if spam_reason:
            logger.info(
                "Spam protection triggered in chat %d on user %d: %s",
                chat_id,
                user.id,
                spam_reason,
            )

            try:
                await message.delete()
            except Exception as exc:
                logger.warning("Could not delete spam message: %s", exc)

            await ModerationActions.warn(chat_id, user, f"Spam detected: {spam_reason}", 0, bot)
            return

    # C. Run AI Moderation (if enabled and text exists)
    if text:
        group_settings = await GroupRepository.get_group(chat_id)
        ai_enabled = bool(group_settings.get("ai_enabled", 1))

        if ai_enabled:
            append_history(chat_id, f"{user.first_name}: {text}")
            recent_context = get_recent_history(chat_id)

            ai_res = await ai_service.analyze_message(text, recent_context, chat_id)
            action = ai_res.get("action", "none")
            reason = ai_res.get("reason", "")
            user_msg = ai_res.get("user_message", "Please respect the chat rules.")

            if action != "none" and not is_admin:
                logger.info(
                    "AI Moderation triggered in chat %d on user %d: action=%s reason=%s",
                    chat_id,
                    user.id,
                    action,
                    reason,
                )

                try:
                    await message.delete()
                except Exception as exc:
                    logger.warning("Could not delete AI flagged message: %s", exc)

                if action == "warn":
                    await ModerationActions.warn(chat_id, user, f"AI Moderation: {reason}", 0, bot)
                    try:
                        sent_msg = await message.answer(f"⚠️ **{user.first_name}**, {user_msg}")
                        task_manager.create_task(
                            auto_delete_message(sent_msg, 20),
                            name=f"autodel_ai_warn_{chat_id}_{user.id}",
                        )
                    except Exception:
                        pass
                elif action == "delete":
                    try:
                        sent_msg = await message.answer(
                            f"⚠️ **{user.first_name}**, please keep chat respectful. Message deleted."
                        )
                        task_manager.create_task(
                            auto_delete_message(sent_msg, 20),
                            name=f"autodel_ai_del_{chat_id}_{user.id}",
                        )
                    except Exception:
                        pass
                elif action == "ban":
                    await ModerationActions.ban(
                        chat_id, user, None, f"AI Moderation: {reason}", 0, bot
                    )
