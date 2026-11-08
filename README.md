# Argus — Telegram group manager

Argus is a Python Telegram group moderation bot. It provides administrator commands, optional AI-assisted moderation, configurable word filters, spam controls, welcome messages and optional join verification. It runs locally or on a server using long polling and SQLite.

## How automatic moderation works

- AI moderation considers recent group text and warns only when the model identifies an unmistakable critical incident (severity 5), such as a credible serious threat. The message is also deleted. Ordinary arguments, profanity, caps, links, and discussion of sensitive topics should not trigger AI action. AI can make mistakes; review warnings with `/warnings` and use `/unwarn` or `/reset` when appropriate.
- Automatic AI warnings do not cause an automatic mute or ban. An administrator can take further action. Manual warnings still escalate at the configured limit: the current implementation mutes for 24 hours when the limit is below 5, or bans when it is 5 or greater. Automatic warnings count toward that limit when a later manual warning is issued.
- During Gemini errors, rate limits, or unavailable contextual analysis, the offline fallback takes no action. It cannot reliably distinguish threats from quotations. Spam detection and administrator-configured filters run separately.
- Spam thresholds can delete messages without issuing warnings. Invite links are deleted by default. Custom filters can explicitly delete, warn, mute or ban, regardless of AI severity. Review existing filters with `/filters` and remove unwanted ones with `/delfilter ID`. These features can still make mistakes.
- AI analyzes text and captions, not image, video or audio content. Group text and recent context are sent to Google's Gemini API when AI is enabled. Tell group members and check your Google account's terms, quotas and billing. `/settings ai off` disables AI per group.

## Setup

1. Install Python 3.10 or newer. In PowerShell, open the project directory and run:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   Copy-Item .env.example .env
   ```

   If PowerShell blocks activation, run `.\.venv\Scripts\python.exe` instead of `python` in the following commands.

2. In Telegram, open [@BotFather](https://t.me/BotFather), send `/newbot` and follow its prompts. Copy the token into `TELEGRAM_TOKEN` in `.env`. Keep it private; anyone with the token can control the bot. If exposed, revoke it through BotFather.
3. Create a Gemini API key in [Google AI Studio](https://aistudio.google.com/apikey), enable any required billing or quota for your project, and put the key in `GEMINI_API_KEY` in `.env`. The configured model must be available to your project; edit `GEMINI_MODEL` and `GEMINI_FALLBACK_MODELS` if necessary. Never commit `.env`.
4. Add the bot to a Telegram **group or supergroup** and promote it to administrator. Grant deletion and restriction/ban permissions for moderation, and pin/change-info permissions if you intend to use those commands. Telegram administrators receive group messages; if running without admin rights, disable privacy mode via BotFather's `/setprivacy` and re-add the bot, but moderation actions still require the appropriate rights. See [Telegram bot settings](https://core.telegram.org/bots/features#botfather).
5. Run `python main.py` and keep the process running. Only one polling instance should use the token; remove an existing webhook if switching from a webhook deployment. Use a process manager or host that restarts the bot after a reboot. SQLite defaults to `warnings.db` in the working directory; back it up along with your configuration, and restrict access to both.
6. In the group, send `/settings` as an administrator and check AI, CAPTCHA and raid settings. Test in a private test group first: verify a normal disagreement is left alone, check `/warnings` and `/unwarn` by replying to a test member, and verify deletion and restriction permissions. Don't test with real threats or harmful material.

`GROUP_CHAT_ID=0` allows all groups; set it to a numeric group ID to limit the message and join/leave moderation pipeline. Commands have their own group and permission checks; do not treat this setting as an access control boundary. Restart after changing `.env`. Only use the bot in groups where you are authorized to manage messages.

## Commands

Reply to a member's message for target-based commands. Moderation commands include `/warn [reason]`, `/unwarn`, `/warnings`, `/reset`, `/mute`, `/tmute 15m`, `/unmute`, `/ban`, `/tban 1d`, `/unban`, `/kick` and `/softban`. `/settings` displays configuration; use `/settings ai off`, `/settings captcha on`, `/settings raid on`, or `/settings limit 3` to change it. CAPTCHA and raid controls may restrict or remove members, so enable them deliberately.

Administrators can use `/filter`, `/filters`, `/delfilter`, `/whitelist`, `/unwhitelist`, `/promote`, `/demote`, `/lock`, `/unlock`, `/pin`, `/unpin`, `/purge`, `/welcome`, `/goodbye`, `/health`, `/status`, `/note`, `/notes` and `/delnote`. Use `/help` for available bot commands and check command replies for syntax. Custom roles are separate from Telegram permissions; the bot itself still needs Telegram administrator rights.

## Troubleshooting and tests

- Bot does not respond: check the process, token, connectivity, group membership, privacy mode and administrator rights. A webhook or second polling process can conflict. Check logs in `logs/` and use `/health`.
- AI takes no action: check `/settings`, key, model availability, quota and logs. On errors, it deliberately falls back to no action. A successful response does not guarantee accurate moderation.
- Cannot delete or restrict: grant the corresponding administrator permissions and check Telegram's restrictions on the target and message age.
- Run `python -m pytest -q` from the project directory. Tests do not replace a live Telegram test.

Configuration is documented in `.env.example`. Do not share tokens, API keys, databases, or logs containing group information.
