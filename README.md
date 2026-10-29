# Groq LinkedIn Voice Agent (Python starter)

A multi-customer starter application for the proposed workflow:

1. A registered customer sends a voice note to a Telegram bot (or types an idea).
2. Groq Whisper transcribes voice; a bounded Groq tool-calling agent reads that customer's style notes, photo descriptions, and published examples.
3. The agent proposes a LinkedIn draft and picks one valid filename from that customer's photo pool.
4. The draft and photo preview are sent to the same Telegram customer.
5. The customer replies `OK` or `OK YYYY-MM-DD HH:MM` to approve, sends text/voice to revise, or replies `CANCEL`.
6. The scheduler only claims a post if `status='scheduled'`, `approved_at IS NOT NULL`, and the publishing time has arrived.
7. The worker uploads the image and publishes through LinkedIn's official REST API. Failures are not blindly retried because a timeout may occur after LinkedIn accepted a post.
8. The bot reminds customers about unapproved drafts and suggests topics if there has been no post activity for a week.

The LLM's tools are read-only. There is deliberately no `publish`, `approve`, or `schedule` tool exposed to the agent. Approval and publishing are deterministic application operations, not model decisions.

## Requirements

- Python 3.11 or newer recommended.
- A Groq API key.
- A Telegram bot token from [@BotFather](https://t.me/BotFather).
- A LinkedIn Developer App with suitable access for Sign In with LinkedIn using OpenID Connect and the Share on LinkedIn permission `w_member_social`. LinkedIn must allow your app to use the relevant API; approval/product access is outside this code.
- A public HTTPS base URL with a LinkedIn redirect URL registered exactly as `PUBLIC_BASE_URL/linkedin/callback` for the OAuth flow. For local testing use a secure tunnel such as ngrok and register the tunnel callback URL in the LinkedIn Developer portal.

## Setup (PowerShell)

```powershell
cd path\to\groq_linkedin_agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` and fill in `GROQ_API_KEY`, `TELEGRAM_BOT_TOKEN`, `PUBLIC_BASE_URL`, `LINKEDIN_CLIENT_ID`, `LINKEDIN_CLIENT_SECRET`, `LINKEDIN_REDIRECT_URI`, and `TOKEN_ENCRYPTION_KEY`.

Generate the token-encryption key once:

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Copy that output to `TOKEN_ENCRYPTION_KEY` in `.env`. **Keep the same key in a secure secret store/back-up. Do not commit `.env`; if the key is lost, saved LinkedIn tokens cannot be decrypted.**

Confirm `LINKEDIN_API_VERSION` is currently supported by LinkedIn's documentation. It is a configurable YYYYMM version and may need to be updated over time.

## Provision a customer / tenant

The bot does not auto-register arbitrary Telegram users. This is deliberate: a customer must be provisioned before private data becomes available to that Telegram account.

1. Start a private chat with your bot and send `/myid`.
2. Copy the numeric Telegram user ID.
3. Run:

```powershell
python -m app.provision_customer --telegram-id 123456789 --name "Alex Example" --timezone Europe/Berlin --style "Direct, warm, analytical, minimal emojis, no exaggerated claims."
```

Replace the example ID/name with the customer’s own values. The script prints a unique photo directory for the customer. Repeat for every customer; each Telegram user ID maps to a different tenant record and photo folder.

## Add photos

Copy the customer's own approved-to-use photos to the directory printed by the provisioning script. Supported extensions: `.jpg`, `.jpeg`, `.png`.

For best photo matching, add a sidecar description next to each image, for example `team_event.jpg.txt` with a short factual description. The app intentionally does not use a vision model; the agent chooses among supplied filenames and descriptions, not by inspecting image pixels. If no valid photo is available, it will refuse to save the draft.

## Run the application

```powershell
python -m app.main
```

- Health check: `http://127.0.0.1:8000/health`
- The bot uses Telegram long polling, so it does not need an inbound webhook.
- LinkedIn OAuth does require the callback URL to be reachable from the public internet over HTTPS.
- For a local demo, run the app on port 8000 and expose it through a secure tunnel; put the tunnel URL in `PUBLIC_BASE_URL` and register the exact callback URL with LinkedIn.

## Connect LinkedIn

From a provisioned customer's private Telegram chat:

1. Send `/connect_linkedin`.
2. Open the one-time link and authorize with LinkedIn.
3. The app reads the signed-in member identity through LinkedIn OpenID Connect and stores the access token encrypted with Fernet.
4. After that, draft approval can schedule publication to that member URN.

The first access token may expire. Refresh is attempted only if LinkedIn returns a refresh token for the app; otherwise reconnect with `/connect_linkedin` when required. LinkedIn app access/scopes and token lifecycle are controlled by LinkedIn, not this application.

## Telegram workflow

- Send a voice note or text to create a draft.
- While there is a draft awaiting approval, a new text or voice note is treated as a correction and resets approval.
- Reply exactly `OK` to approve and schedule it for the next weekday at 09:00 in the customer's timezone.
- Or reply `OK 2026-11-02 10:30` to approve and use a chosen future local time (`YYYY-MM-DD HH:MM`).
- Reply `CANCEL` to discard a draft awaiting approval.
- Use `RESCHEDULE 2026-11-03 11:00` to reschedule an already-approved post that has not started publishing.
- `/status` shows recent post states; `/help` repeats instructions.

No reply means no approval. The app can send a reminder, but it never publishes an unapproved draft.

## Run tests

```powershell
python -m pytest -q
```

## Main files

- `app/groq_agent.py` — Groq transcription and bounded tool-calling agent.
- `app/telegram_bot.py` — private Telegram interactions, approval/correction commands.
- `app/linkedin.py` — token handling, image upload, and LinkedIn Posts API.
- `app/oauth.py` — per-customer LinkedIn authorization-code flow with one-time link and OAuth state.
- `app/db.py` — SQLite tenant/post/OAuth state and approval invariants.
- `app/scheduler.py` — due posts, reminders, weekly topic suggestions, and no-blind-retry behavior.

## Security, reliability, and production notes

This is a learning/demo starter, **not production-hardened software**. Before handling real customers:

- Replace SQLite with PostgreSQL and add backups, migrations, and transaction monitoring.
- Store secrets in a managed secret store and use encrypted storage/backups; keep image pools in tenant-scoped object storage with least-privilege access.
- Add authenticated admin/customer management, structured audit records, retention/deletion controls, rate limiting, monitoring, and incident response.
- Review GDPR roles and lawful basis, privacy notices, processor agreements, international data transfers, and retention with the responsible organization. Voice notes and post drafts may contain personal or confidential information and are sent to Telegram/Groq/LinkedIn as part of processing.
- Ensure customers have rights and permission for photos, and that photo captions are accurate.
- Review LinkedIn developer terms, permissions, rate limits, API versions, and any required access approvals before going live.
- Run one app instance only with this SQLite + Telegram polling setup. Multiple replicas need a shared database, distributed scheduler/locking, and webhook or single-consumer coordination.
- External posting requests have an unavoidable ambiguity if the network fails after LinkedIn accepts the request but before the app receives a response. This app marks those attempts for manual review rather than automatically reposting and risking duplicates.
- The photo picker relies on sidecar text and filename metadata, not image understanding. For production, add an authorized vision model or a human-maintained asset catalog if content-based selection is required.
- Never treat an LLM response as user approval. Approval is a separate Telegram message and is enforced in database state before the publisher can claim the post.
