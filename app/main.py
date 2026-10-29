from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from app.config import SCHEDULER_INTERVAL_SECONDS, TELEGRAM_BOT_TOKEN
from app.db import init_db
from app.oauth import router as oauth_router
from app.scheduler import scheduler_loop
from app.telegram_bot import build_telegram_application

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)

telegram_application = None
scheduler_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global telegram_application, scheduler_task
    init_db()
    if TELEGRAM_BOT_TOKEN:
        telegram_application = build_telegram_application(TELEGRAM_BOT_TOKEN)
        await telegram_application.initialize()
        await telegram_application.start()
        if telegram_application.updater is None:
            raise RuntimeError("Telegram application updater is not available")
        await telegram_application.updater.start_polling(drop_pending_updates=False)
        scheduler_task = asyncio.create_task(
            scheduler_loop(telegram_application.bot, SCHEDULER_INTERVAL_SECONDS),
            name="linkedin-approval-and-publishing-scheduler",
        )
        logging.getLogger(__name__).info("Telegram polling and scheduling started")
    else:
        logging.getLogger(__name__).warning(
            "TELEGRAM_BOT_TOKEN is empty: API/OAuth routes are available, but bot and scheduler are disabled."
        )
    try:
        yield
    finally:
        if scheduler_task:
            scheduler_task.cancel()
            try:
                await scheduler_task
            except asyncio.CancelledError:
                pass
        if telegram_application:
            if telegram_application.updater and telegram_application.updater.running:
                await telegram_application.updater.stop()
            await telegram_application.stop()
            await telegram_application.shutdown()
        telegram_application = None
        scheduler_task = None


app = FastAPI(
    title="Groq LinkedIn Voice Agent",
    description="Multi-customer draft, approval, scheduling, and LinkedIn publishing workflow.",
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(oauth_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "groq-linkedin-voice-agent"}


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)
