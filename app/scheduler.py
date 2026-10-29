from __future__ import annotations

import asyncio
import logging
from typing import Any

from telegram import Bot

from app.config import APPROVAL_TIMEOUT_HOURS
from app.db import (
    approval_reminders_due,
    claim_post_for_publishing,
    customers_due_for_topic_nudge,
    fail_publishing,
    finish_published,
    get_customer_by_id,
    get_due_posts,
    mark_approval_reminded,
    mark_topic_nudge_sent,
    mark_stale_publishing_as_uncertain,
)
from app.groq_agent import suggest_topic_ideas
from app.linkedin import publish_to_linkedin
from app.scheduling import format_local

logger = logging.getLogger(__name__)


async def run_maintenance_cycle(bot: Bot | None) -> None:
    # First claim due jobs in the DB. Only a post with approved_at set can be claimed.
    for post in get_due_posts():
        if not claim_post_for_publishing(post["id"], post["customer_id"]):
            continue
        customer = get_customer_by_id(post["customer_id"])
        if not customer:
            fail_publishing(post["id"], post["customer_id"], "Customer record no longer exists")
            continue
        try:
            linkedin_post_id = await publish_to_linkedin(customer, post)
            finish_published(post["id"], customer["id"], linkedin_post_id)
            if bot:
                await bot.send_message(
                    chat_id=customer["telegram_user_id"],
                    text=f"Published successfully to your LinkedIn account. Post reference: {linkedin_post_id}",
                )
        except Exception as exc:
            logger.exception("LinkedIn publication failed for post %s", post["id"])
            fail_publishing(post["id"], customer["id"], f"{type(exc).__name__}: {exc}")
            if bot:
                try:
                    await bot.send_message(
                        chat_id=customer["telegram_user_id"],
                        text=(
                            "I could not confirm successful LinkedIn publication. The post has been stopped for manual review "
                            "and will not be retried automatically, to avoid duplicate posts. Check LinkedIn before trying again. "
                            f"Error category: {type(exc).__name__}. Use /status or contact the administrator."
                        ),
                    )
                except Exception:
                    logger.warning("Could not notify customer about failed publication")

    if not bot:
        return

    # Remind, but never auto-approve a draft due to silence.
    for item in approval_reminders_due(APPROVAL_TIMEOUT_HOURS):
        try:
            await bot.send_message(
                chat_id=item["telegram_user_id"],
                text=(
                    "Reminder: a LinkedIn draft is still awaiting your approval. Nothing will be published without your explicit OK. "
                    "Reply OK to approve, send a correction, or CANCEL to discard it."
                ),
            )
            mark_approval_reminded(item["id"], item["customer_id"])
        except Exception:
            logger.warning("Could not send approval reminder")

    # Weekly topic suggestions are advisory only; they never create or publish a post.
    for customer in customers_due_for_topic_nudge():
        try:
            try:
                ideas = await suggest_topic_ideas(customer)
            except Exception:
                logger.exception("Topic suggestions unavailable; using generic prompts")
                ideas = [
                    "A lesson you learned from a recent project",
                    "A common misconception in your industry",
                    "A small process improvement that made a difference",
                ]
            body = "\n".join(f"{i + 1}. {idea}" for i, idea in enumerate(ideas))
            await bot.send_message(
                chat_id=customer["telegram_user_id"],
                text=(
                    "It's been a week since your last post idea. Here are three possible topics you could talk about in a short voice message:\n\n"
                    f"{body}\n\nSend a voice note whenever you're ready. These are suggestions only; nothing will be published automatically."
                ),
            )
            mark_topic_nudge_sent(customer["id"])
        except Exception:
            logger.warning("Could not send weekly topic ideas")


async def scheduler_loop(bot: Bot | None, interval_seconds: int) -> None:
    mark_stale_publishing_as_uncertain(age_minutes=30)
    while True:
        try:
            await run_maintenance_cycle(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Scheduled maintenance cycle failed")
        await asyncio.sleep(max(10, interval_seconds))
