"""
Scheduler service running periodic background tasks like lifting expired mutes/bans.
"""

import asyncio
import logging
from typing import Any, Optional
from datetime import datetime, timezone
from database.models import PunishmentRepository

logger = logging.getLogger("argus.scheduler")


class SchedulerService:
    """Manages periodic async jobs (e.g. temporary ban/mute expiration)."""

    def __init__(self) -> None:
        self.bot = None  # Injected at bot startup to avoid circular imports
        self._task: Optional[asyncio.Task] = None
        self._running = False

    def start(self, bot_instance: Any) -> None:
        """Start the background scheduler loop."""
        self.bot = bot_instance
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Scheduler service started.")

    async def stop(self) -> None:
        """Stop the background scheduler loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Scheduler service stopped.")

    async def _loop(self) -> None:
        """Main periodic loop."""
        while self._running:
            try:
                await self._check_expirations()
            except Exception as exc:
                logger.error("Error in scheduler expiration job: %s", exc, exc_info=True)
            
            # Check every 30 seconds
            await asyncio.sleep(30)

    async def _check_expirations(self) -> None:
        """Scan punishments database for expired temp restrictions."""
        if not self.bot:
            return

        expired = await PunishmentRepository.get_expired_punishments()
        if not expired:
            return

        for p in expired:
            chat_id = p["chat_id"]
            user_id = p["user_id"]
            action = p["action"]
            p_id = p["id"]

            logger.info("Lifting expired restriction %s for user %d in chat %d", action, user_id, chat_id)
            
            try:
                if action == "mute":
                    # Unmute user (grant default chat permissions or set custom permissions)
                    # To unmute, we restrict chat member with full permissions
                    from aiogram.types import ChatPermissions
                    unmute_perms = ChatPermissions(
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
                        can_change_info=False,
                        can_invite_users=True,
                        can_pin_messages=False,
                    )
                    await self.bot.restrict_chat_member(
                        chat_id=chat_id,
                        user_id=user_id,
                        permissions=unmute_perms,
                    )
                    await self.bot.send_message(
                        chat_id,
                        f"🔊 Temporary mute has expired. User {user_id} has been unmuted."
                    )
                    
                elif action == "ban":
                    # Unban user (which allows them to join back, but doesn't auto-add them)
                    await self.bot.unban_chat_member(chat_id=chat_id, user_id=user_id, only_if_banned=True)
                    await self.bot.send_message(
                        chat_id,
                        f"✅ Temporary ban has expired. User {user_id} has been unbanned."
                    )

                # Mark punishment as inactive
                await PunishmentRepository.revoke_active_punishments(chat_id, user_id, action)
                logger.info("Successfully revoked active punishment ID %d in DB", p_id)
                
            except Exception as exc:
                logger.error("Failed to lift punishment ID %d for user %d: %s", p_id, user_id, exc)


# Singleton instance
scheduler_service = SchedulerService()
