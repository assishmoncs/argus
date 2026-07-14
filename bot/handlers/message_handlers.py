"""
Message moderation pipeline: filters, spam protection, AI analysis, welcomes, and Captcha verification.
"""

import time
import asyncio
import logging
from typing import Dict, List, Any
from aiogram import Router, F, Bot, types
from aiogram.enums import ChatMemberStatus
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config.settings import settings
from database.models import (
    GroupRepository,
    UserRepository,
    GroupMemberRepository,
    WarningRepository,
    PunishmentRepository,
)
from services.filter_service import filter_service
from services.spam_service import spam_service
from services.ai_service import ai_service
from moderation.actions import ModerationActions, auto_delete_message

logger = logging.getLogger("argus.handlers.messages")
router = Router()

# In-memory sliding-window history for AI context per chat
# Format: {chat_id: [username: message_text]}
_chat_histories: Dict[int, List[str]] = {}
MAX_HISTORY = 10

# Pending CAPTCHA verifications: { (chat_id, user_id): (captcha_msg_id, join_timestamp) }
_pending_captchas: Dict[tuple[int, int], tuple[int, float]] = {}


def get_recent_history(chat_id: int) -> str:
    """Format recent messages in chat for AI context."""
    history = _chat_histories.get(chat_id, [])
    if not history:
        return "No recent messages."
    return "\n".join(history)


def append_history(chat_id: int, formatted_msg: str) -> None:
    """Append a message to the AI context sliding window."""
    if chat_id not in _chat_histories:
        _chat_histories[chat_id] = []
    _chat_histories[chat_id].append(formatted_msg)
    if len(_chat_histories[chat_id]) > MAX_HISTORY:
        _chat_histories[chat_id].pop(0)


# ---------------------------------------------------------------------------
# Welcomes & CAPTCHA Verification
# ---------------------------------------------------------------------------

@router.message(F.new_chat_members)
async def on_user_join(message: types.Message, bot: Bot):
    """Handle new members joining. Trigger mute, CAPTCHA challenge, and welcome message."""
    chat_id = message.chat.id
    group_settings = await GroupRepository.get_group(chat_id)
    captcha_enabled = bool(group_settings.get("welcome_captcha", 0))
    anti_raid = bool(group_settings.get("anti_raid", 0))

    # Anti-raid join rate check
    if spam_service.record_join(chat_id, anti_raid):
        # Raid detected! Lock group down: set welcome_captcha to True and alert admins
        await message.answer("🚨 **Raid Warning**: High joining rate detected! Activating mandatory CAPTCHA lockdown.")
        captcha_enabled = True

    for member in message.new_chat_members:
        if member.is_bot:
            # Kick bots unless they are specifically allowed
            await bot.ban_chat_member(chat_id, member.id)
            await bot.unban_chat_member(chat_id, member.id)
            continue

        # Register user in DB
        await UserRepository.get_or_create_user(
            member.id, member.username, member.first_name, member.last_name
        )
        await GroupMemberRepository.set_member_role(chat_id, member.id, "member")

        if captcha_enabled:
            # Mute user immediately
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

            # Send CAPTCHA
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Verify - I am a Human!", callback_data=f"captcha_verify:{member.id}")
            ]])
            
            captcha_msg = await message.answer(
                f"🛡️ **Welcome, {member.first_name}!**\n"
                f"To prevent spam bots, please click the button below within 90 seconds to unmute yourself.",
                reply_markup=keyboard,
            )
            
            # Record pending captcha
            key = (chat_id, member.id)
            _pending_captchas[key] = (captcha_msg.message_id, time.time())
            
            # Start a background timer to kick if not verified
            asyncio.create_task(verify_captcha_timeout(chat_id, member, captcha_msg.message_id, bot))
        else:
            # Regular welcome message
            welcome_template = group_settings.get("welcome_message", "Welcome to the group!")
            welcome_text = welcome_template.replace("[first_name]", member.first_name)
            welcome_text = welcome_text.replace("[username]", f"@{member.username}" if member.username else member.first_name)
            welcome_text = welcome_text.replace("[group_title]", message.chat.title or "this group")
            await message.answer(welcome_text)


async def verify_captcha_timeout(chat_id: int, user: types.User, captcha_msg_id: int, bot: Bot):
    """Wait 90 seconds. If user is still in _pending_captchas, kick them."""
    await asyncio.sleep(90)
    key = (chat_id, user.id)
    if key in _pending_captchas and _pending_captchas[key][0] == captcha_msg_id:
        logger.info("CAPTCHA timeout for user %d in chat %d. Kicking...", user.id, chat_id)
        # Delete CAPTCHA message
        try:
            await bot.delete_message(chat_id, captcha_msg_id)
        except Exception:
            pass
        # Kick user
        try:
            await bot.ban_chat_member(chat_id, user.id)
            await bot.unban_chat_member(chat_id, user.id)
            await bot.send_message(chat_id, f"🚫 **{user.first_name}** failed CAPTCHA verification and was kicked.")
        except Exception as exc:
            logger.error("Failed to kick user on CAPTCHA timeout: %s", exc)
        
        # Remove from pending dict
        _pending_captchas.pop(key, None)


@router.callback_query(F.data.startswith("captcha_verify:"))
async def on_captcha_click(callback: types.CallbackQuery, bot: Bot):
    """Handle verification click."""
    user_id = int(callback.data.split(":")[1])
    clicker = callback.from_user
    chat_id = callback.message.chat.id

    if clicker.id != user_id:
        await callback.answer("❌ This verification prompt is not for you!", show_alert=True)
        return

    key = (chat_id, user_id)
    if key in _pending_captchas:
        # Unmute user
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
            # Delete captcha message
            await callback.message.delete()
        except Exception as exc:
            logger.error("Failed to restore permissions on CAPTCHA pass: %s", exc)
            await callback.answer("❌ Failed to verify. Contact admins.", show_alert=True)

        _pending_captchas.pop(key, None)

        # Print welcome message
        group_settings = await GroupRepository.get_group(chat_id)
        welcome_template = group_settings.get("welcome_message", "Welcome to the group!")
        welcome_text = welcome_template.replace("[first_name]", clicker.first_name)
        welcome_text = welcome_text.replace("[username]", f"@{clicker.username}" if clicker.username else clicker.first_name)
        welcome_text = welcome_text.replace("[group_title]", callback.message.chat.title or "this group")
        await bot.send_message(chat_id, welcome_text)
    else:
        await callback.answer("⚠️ Your captcha session has expired or is invalid.", show_alert=True)


@router.message(F.left_chat_member)
async def on_user_leave(message: types.Message):
    """Handle users leaving/being kicked. Send goodbye message."""
    chat_id = message.chat.id
    member = message.left_chat_member
    
    # Deactivate group member record
    await GroupMemberRepository.set_member_role(chat_id, member.id, "banned" if message.from_user.id != member.id else "member")

    group_settings = await GroupRepository.get_group(chat_id)
    goodbye_template = group_settings.get("goodbye_message", "Goodbye!")
    goodbye_text = goodbye_template.replace("[first_name]", member.first_name)
    goodbye_text = goodbye_text.replace("[username]", f"@{member.username}" if member.username else member.first_name)
    goodbye_text = goodbye_text.replace("[group_title]", message.chat.title or "this group")
    await message.answer(goodbye_text)


# ---------------------------------------------------------------------------
# Message Moderation Pipeline
# ---------------------------------------------------------------------------

@router.message()
async def on_message(message: types.Message, bot: Bot):
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

    # Save user info in database
    await UserRepository.get_or_create_user(
        user.id, user.username, user.first_name, user.last_name
    )

    # Admins/Creator are exempt from spam and filter checks
    is_admin = False
    try:
        member = await bot.get_chat_member(chat_id, user.id)
        if member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
            is_admin = True
    except Exception:
        pass

    text = (message.text or message.caption or "").strip()

    if not is_admin:
        # A. Run Banned Words Filters
        filter_result = await filter_service.check_message(chat_id, text)
        if filter_result:
            action = filter_result["action"]
            reason = filter_result["reason"]
            logger.info("Filter triggered in chat %d on user %d: action=%s reason=%s", chat_id, user.id, action, reason)
            
            try:
                await message.delete()
            except Exception as exc:
                logger.warning("Could not delete message: %s", exc)

            if action == "warn":
                await ModerationActions.warn(chat_id, user, reason, 0, bot)
            elif action == "mute":
                await ModerationActions.mute(chat_id, user, 3600, reason, 0, bot)  # default 1 hr
            elif action == "ban":
                await ModerationActions.ban(chat_id, user, None, reason, 0, bot)
            return

        # B. Run Spam Protection checks
        spam_reason = await spam_service.check_spam(chat_id, user.id, message)
        if spam_reason:
            logger.info("Spam protection triggered in chat %d on user %d: %s", chat_id, user.id, spam_reason)
            
            try:
                await message.delete()
            except Exception as exc:
                logger.warning("Could not delete spam message: %s", exc)

            # Auto warn/mute depending on severity. Let's warn them!
            await ModerationActions.warn(chat_id, user, f"Spam detected: {spam_reason}", 0, bot)
            return

    # C. Run AI Moderation (if enabled and text exists)
    if text:
        group_settings = await GroupRepository.get_group(chat_id)
        ai_enabled = bool(group_settings.get("ai_enabled", 1))

        if ai_enabled:
            # Add message to sliding context window
            append_history(chat_id, f"{user.first_name}: {text}")
            recent_context = get_recent_history(chat_id)

            # Call AI Service analysis
            ai_res = await ai_service.analyze_message(text, recent_context, chat_id)
            action = ai_res.get("action", "none")
            reason = ai_res.get("reason", "")
            user_msg = ai_res.get("user_message", "Please respect the chat rules.")

            if action != "none" and not is_admin:
                logger.info("AI Moderation triggered in chat %d on user %d: action=%s reason=%s", chat_id, user.id, action, reason)
                
                try:
                    await message.delete()
                except Exception as exc:
                    logger.warning("Could not delete AI flagged message: %s", exc)

                if action == "warn":
                    await ModerationActions.warn(chat_id, user, f"AI Moderation: {reason}", 0, bot)
                    # Send alert text to user
                    sent_msg = await message.answer(f"⚠️ **{user.first_name}**, {user_msg}")
                    asyncio.create_task(auto_delete_message(sent_msg, 20))
                elif action == "delete":
                    sent_msg = await message.answer(f"⚠️ **{user.first_name}**, please keep chat respectful. Message deleted.")
                    asyncio.create_task(auto_delete_message(sent_msg, 20))
                elif action == "ban":
                    await ModerationActions.ban(chat_id, user, None, f"AI Moderation: {reason}", 0, bot)
