"""
Scheduler service running periodic background tasks including lifting expired mutes/bans,
memory/cache garbage collection, and extensible custom periodic jobs.
"""

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, Optional

from aiogram.types import ChatPermissions

from config.settings import settings
from database.models import GroupMemberRepository, PunishmentRepository
from services.ai_service import ai_service
from services.spam_service import spam_service
from utils.telemetry import telemetry

logger = logging.getLogger("argus.scheduler")


class SchedulerService:
    """Manages periodic async jobs with concurrency bounds and error isolation."""

    def __init__(self, interval_seconds: Optional[float] = None) -> None:
        self.bot: Any = None
        self.interval_seconds = (
            interval_seconds if interval_seconds is not None else settings.SCHEDULER_INTERVAL_SECONDS
        )
        self._task: Optional[asyncio.Task[None]] = None
        self._running = False
        self._semaphore = asyncio.Semaphore(10)
        self._custom_jobs: Dict[str, Callable[[], Awaitable[None]]] = {}

    def register_job(self, name: str, coro_func: Callable[[], Awaitable[None]]) -> None:
        """Register an additional periodic background coroutine to run on each tick."""
        self._custom_jobs[name] = coro_func

    def unregister_job(self, name: str) -> None:
        """Remove a registered periodic background job."""
        self._custom_jobs.pop(name, None)

    def start(self, bot_instance: Any) -> None:
        """Start the background scheduler loop idempotently."""
        self.bot = bot_instance
        if self._running and self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="argus_scheduler_loop")
        logger.info("Scheduler service started (interval=%.1fs).", self.interval_seconds)

    async def stop(self) -> None:
        """Stop the background scheduler loop cleanly."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Scheduler service stopped.")

    async def _loop(self) -> None:
        """Main periodic loop."""
        while self._running:
            try:
                await self.run_tick()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                telemetry.increment("scheduler_errors_total")
                logger.error("Error in scheduler loop: %s", exc, exc_info=True)

            await asyncio.sleep(self.interval_seconds)

    async def run_tick(self) -> None:
        """Execute a single scheduler cycle (punishment expirations + memory cleanup + custom jobs)."""
        async with telemetry.async_timer("scheduler_tick_latency"):
            await self._check_expirations()
            spam_service.cleanup_stale()
            ai_service.cache.evict_expired()

            for job_name, job_fn in list(self._custom_jobs.items()):
                try:
                    await job_fn()
                except Exception as exc:
                    telemetry.increment("scheduler_job_errors_total", job=job_name)
                    logger.error("Custom scheduler job '%s' failed: %s", job_name, exc, exc_info=True)

    async def _lift_single_punishment(self, p: dict[str, Any]) -> None:
        """Lift a single expired punishment under bounded concurrency."""
        async with self._semaphore:
            chat_id = p["chat_id"]
            user_id = p["user_id"]
            action = p["action"]
            p_id = p["id"]

            logger.info(
                "Lifting expired restriction %s for user %d in chat %d",
                action,
                user_id,
                chat_id,
            )

            try:
                if action == "mute":
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
                        f"🔊 Temporary mute has expired. User {user_id} has been unmuted.",
                    )
                    await GroupMemberRepository.set_member_role(chat_id, user_id, "member")

                elif action == "ban":
                    await self.bot.unban_chat_member(
                        chat_id=chat_id, user_id=user_id, only_if_banned=True
                    )
                    await self.bot.send_message(
                        chat_id,
                        f"✅ Temporary ban has expired. User {user_id} has been unbanned.",
                    )
                    await GroupMemberRepository.set_member_role(chat_id, user_id, "member")

                await PunishmentRepository.revoke_active_punishments(chat_id, user_id, action)
                telemetry.increment("scheduler_expirations_lifted_total", action=action)
                logger.info("Successfully revoked active punishment ID %d in DB", p_id)

            except Exception as exc:
                telemetry.increment("scheduler_errors_total", action=action)
                logger.error(
                    "Failed to lift punishment ID %d for user %d: %s", p_id, user_id, exc
                )

    async def _check_expirations(self) -> None:
        """Scan punishments database for expired temp restrictions and lift concurrently."""
        if not self.bot:
            return

        expired = await PunishmentRepository.get_expired_punishments()
        if not expired:
            return

        await asyncio.gather(*(self._lift_single_punishment(p) for p in expired))


# Singleton instance
scheduler_service = SchedulerService()
