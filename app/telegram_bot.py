from __future__ import annotations

import asyncio
import logging
import re
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from app.config import (
    APPROVAL_TIMEOUT_HOURS,
    GROQ_API_KEY,
    LINKEDIN_CLIENT_ID,
    LINKEDIN_CLIENT_SECRET,
    MAX_VOICE_FILE_BYTES,
    PUBLIC_BASE_URL,
)
from app.db import (
    approve_and_schedule,
    cancel_pending_post,
    create_connect_code,
    create_post,
    get_customer_by_telegram_id,
    get_latest_scheduled_post,
    get_pending_post,
    recent_post_statuses,
    reschedule_post,
    update_post_draft,
)
from app.groq_agent import create_or_revise_draft, transcribe_audio_file
from app.photos import resolve_customer_photo
from app.scheduling import default_scheduled_time, format_local, parse_user_schedule

logger = logging.getLogger(__name__)


def build_telegram_application(token: str) -> Application:
    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("myid", myid_command))
    application.add_handler(CommandHandler("connect_linkedin", connect_linkedin_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.VOICE, voice_message))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))
    application.add_error_handler(error_handler)
    return application


async def _registered_customer(update: Update):
    if not update.effective_user or not update.effective_chat:
        return None
    if update.effective_chat.type != "private":
        if update.effective_message:
            await update.effective_message.reply_text("Please use this bot in a private chat for customer-data separation.")
        return None
    customer = get_customer_by_telegram_id(update.effective_user.id)
    if not customer and update.effective_message:
        await update.effective_message.reply_text(
            "This Telegram account is not registered for the service yet. "
            "Use /myid and send that numeric ID to the administrator for provisioning."
        )
    return customer


async def myid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat and update.effective_chat.type == "private" and update.effective_user:
        await update.effective_message.reply_text(f"Your Telegram user ID is: {update.effective_user.id}")
    elif update.effective_message:
        await update.effective_message.reply_text("Please use /myid in a private chat.")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    customer = await _registered_customer(update)
    if not customer:
        return
    await update.effective_message.reply_text(
        f"Hello {customer['display_name']}! Send a voice message (or text) with an idea for a LinkedIn post.\n\n"
        "I will create a draft and select a photo from your own photo pool. Nothing is published until you explicitly reply OK.\n\n"
        "Commands: /connect_linkedin, /status, /help.\n"
        "After reviewing a draft, reply OK to schedule it for the next weekday at 09:00 in your timezone, or reply "
        "OK YYYY-MM-DD HH:MM to choose a time. Send corrections as text or a voice message."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    customer = await _registered_customer(update)
    if not customer:
        return
    await update.effective_message.reply_text(
        "Workflow help\n"
        "• Send a voice message or text to create a post draft.\n"
        "• While a draft is awaiting approval, another text/voice message is treated as a correction.\n"
        "• Reply exactly OK to approve and schedule for the next weekday at 09:00, or OK YYYY-MM-DD HH:MM.\n"
        "• Reply CANCEL to discard a draft awaiting approval.\n"
        "• Use RESCHEDULE YYYY-MM-DD HH:MM to move a scheduled post before it publishes.\n"
        "• /connect_linkedin connects your own account; /status shows recent post states.\n\n"
        "A model-generated draft is never treated as approval."
    )


async def connect_linkedin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    customer = await _registered_customer(update)
    if not customer:
        return
    if not (PUBLIC_BASE_URL and LINKEDIN_CLIENT_ID and LINKEDIN_CLIENT_SECRET):
        await update.effective_message.reply_text(
            "LinkedIn OAuth is not configured by the administrator yet. The service needs a public HTTPS URL and a LinkedIn Developer App."
        )
        return
    code = create_connect_code(customer["id"])
    url = f"{PUBLIC_BASE_URL}/linkedin/connect?code={code}"
    await update.effective_message.reply_text(
        "Open this one-time link in your browser to connect your own LinkedIn account. It expires in 15 minutes:\n\n"
        f"{url}\n\n"
        "Review the LinkedIn authorization screen carefully. The app will ask for permission to publish on your behalf; every post still requires your explicit approval."
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    customer = await _registered_customer(update)
    if not customer:
        return
    statuses = recent_post_statuses(customer["id"], limit=5)
    linked = bool(customer.get("linkedin_member_urn") and customer.get("linkedin_access_token_enc"))
    lines = [f"LinkedIn account connected: {'yes' if linked else 'no — use /connect_linkedin'}", "Recent posts:"]
    if not statuses:
        lines.append("No posts yet.")
    for post in statuses:
        line = f"• {post['status']} (revision {post['revision']})"
        if post.get("scheduled_at"):
            line += f" — {format_local(post['scheduled_at'], customer['timezone'])}"
        if post.get("last_error"):
            line += " — needs manual check"
        lines.append(line)
    await update.effective_message.reply_text("\n".join(lines))


async def voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    customer = await _registered_customer(update)
    if not customer:
        return
    voice = update.effective_message.voice
    if not voice:
        return
    if voice.file_size and voice.file_size > MAX_VOICE_FILE_BYTES:
        await update.effective_message.reply_text("That voice message is too large. Please send a shorter recording.")
        return
    if not GROQ_API_KEY:
        await update.effective_message.reply_text("Groq is not configured. Please contact the administrator.")
        return

    pending = get_pending_post(customer["id"])
    await update.effective_message.reply_text("Transcribing your voice message and preparing the draft…")
    temp_path: Path | None = None
    try:
        telegram_file = await context.bot.get_file(voice.file_id)
        with tempfile.NamedTemporaryFile(prefix="voice_", suffix=".ogg", delete=False) as tmp:
            temp_path = Path(tmp.name)
        await telegram_file.download_to_drive(custom_path=temp_path)
        if temp_path.stat().st_size > MAX_VOICE_FILE_BYTES:
            raise ValueError("Downloaded voice message exceeds the configured size limit")
        transcript = await asyncio.to_thread(transcribe_audio_file, temp_path)
        await _process_input(
            update=update,
            context=context,
            customer=customer,
            source_text=transcript,
            pending=pending,
            correction_mode=bool(pending),
        )
    except Exception as exc:
        logger.exception("Voice processing failed for registered customer")
        message = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else "An unexpected service error occurred."
        await update.effective_message.reply_text(f"I could not process that voice message. {message}")
    finally:
        if temp_path:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not delete temporary voice file")


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    customer = await _registered_customer(update)
    if not customer:
        return
    text = (update.effective_message.text or "").strip()
    if not text:
        return
    pending = get_pending_post(customer["id"])

    if pending and re.fullmatch(r"OK(?:\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})?", text, flags=re.IGNORECASE):
        if not customer.get("linkedin_member_urn") or not customer.get("linkedin_access_token_enc"):
            await update.effective_message.reply_text("Please connect your LinkedIn account first with /connect_linkedin. The draft remains unapproved.")
            return
        try:
            scheduled = parse_user_schedule(text, customer["timezone"], prefix="OK") if len(text.split()) > 1 else None
            scheduled = scheduled or default_scheduled_time(customer["timezone"])
        except ValueError as exc:
            await update.effective_message.reply_text(str(exc))
            return
        result = approve_and_schedule(pending["id"], customer["id"], scheduled)
        if not result:
            await update.effective_message.reply_text("I couldn't approve this draft because its state changed. Please use /status.")
            return
        await update.effective_message.reply_text(
            "Approved and scheduled. I will publish the approved text and photo at "
            f"{scheduled.strftime('%Y-%m-%d %H:%M %Z')} (your local time). "
            "If a service fails, I will alert you and will not retry an uncertain LinkedIn publish automatically."
        )
        return

    if pending and text.upper() in {"CANCEL", "REJECT", "DISCARD"}:
        if cancel_pending_post(pending["id"], customer["id"]):
            await update.effective_message.reply_text("Draft cancelled. Nothing was published.")
        else:
            await update.effective_message.reply_text("The draft state changed; use /status to check it.")
        return

    if not pending and re.fullmatch(r"RESCHEDULE\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}", text, flags=re.IGNORECASE):
        scheduled_post = get_latest_scheduled_post(customer["id"])
        if not scheduled_post:
            await update.effective_message.reply_text("You have no scheduled post to reschedule.")
            return
        try:
            scheduled = parse_user_schedule(text, customer["timezone"], prefix="RESCHEDULE")
        except ValueError as exc:
            await update.effective_message.reply_text(str(exc))
            return
        if not scheduled or not reschedule_post(scheduled_post["id"], customer["id"], scheduled):
            await update.effective_message.reply_text("I couldn't reschedule that post. Check /status; it may already be publishing.")
            return
        await update.effective_message.reply_text(f"Post rescheduled to {scheduled.strftime('%Y-%m-%d %H:%M %Z')}.")
        return

    if not pending and text.upper() == "OK":
        await update.effective_message.reply_text("There is no draft awaiting approval. Send a voice message or text idea first.")
        return

    await _process_input(
        update=update,
        context=context,
        customer=customer,
        source_text=text,
        pending=pending,
        correction_mode=bool(pending),
    )


async def _process_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    customer: dict,
    source_text: str,
    pending: dict | None,
    correction_mode: bool,
) -> None:
    if not source_text.strip():
        await update.effective_message.reply_text("I did not receive any usable text. Please try again.")
        return
    try:
        result = await create_or_revise_draft(
            customer=customer,
            transcript=source_text.strip(),
            existing_draft=pending["draft_text"] if correction_mode and pending else None,
            correction_mode=correction_mode,
        )
        if len(result["content"]) > 3000:
            await update.effective_message.reply_text("The draft is longer than the LinkedIn post limit. Please send a shorter idea or correction.")
            return
        photo = resolve_customer_photo(customer, result["photo_filename"])
        if correction_mode and pending:
            post = update_post_draft(
                pending["id"], customer["id"], source_text.strip(), result["content"], str(photo)
            )
        else:
            post = create_post(customer["id"], source_text.strip(), result["content"], str(photo))

        revision_line = f" (revision {post['revision']})" if correction_mode else ""
        await update.effective_message.reply_text(
            f"DRAFT{revision_line} — awaiting your approval\n\n{result['content']}\n\n"
            "Reply exactly OK to approve and schedule for the next weekday at 09:00, or "
            "OK YYYY-MM-DD HH:MM to choose the publishing time. Send a text/voice correction to revise it, or CANCEL to discard it.\n\n"
            f"Photo choice: {result['photo_filename']}\nReason: {result['rationale'] or 'Selected from your own photo pool.'}"
        )
        with photo.open("rb") as image_file:
            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=image_file,
                caption=f"Proposed photo for draft {post['id'][:8]}. This is a preview only; nothing has been published.",
            )
    except Exception as exc:
        logger.exception("Draft generation failed for registered customer")
        detail = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else "An unexpected service error occurred."
        await update.effective_message.reply_text(
            "I could not create the draft. Check that your customer photo pool has images (.jpg, .jpeg, .png, or .webp) "
            "and that Groq is configured. " + detail
        )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Telegram handler error: %s", type(context.error).__name__ if context.error else "unknown")
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("Something went wrong while processing that message. Please try again or contact the administrator.")
        except Exception:
            pass
