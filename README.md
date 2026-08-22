# Telegram post renderer

A tiny private Telegram bot: send it a UTF-8 `*.tgpost.html` document and it replies in the same chat with Telegram-native HTML formatting. Copy that rendered message into your channel manually.

It only accepts documents named exactly `*.tgpost.html` from the configured user ID. It ignores every other update. It never publishes to a channel.

## Post format

The file must contain only HTML supported by the [Telegram Bot API](https://core.telegram.org/bots/api#html-style), for example:

```html
<b>Open-source alternatives</b>

<b>Twenty</b>
https://github.com/twentyhq/twenty

Replaces: <code>Salesforce</code>
<pre><code class="language-powershell">gh repo clone twentyhq/twenty</code></pre>
```

The bot sends this source unchanged with `parse_mode=HTML`; Telegram performs the formatting. Link previews are disabled. Files longer than 4,096 characters are rejected rather than split.

## Run locally (PowerShell)

Create a virtual environment and install dependencies:

```powershell
py -3.12 -m venv .venv
.\\.venv\\Scripts\\Activate.ps1
pip install -r requirements.txt
```

Copy `.env.example` to `.env`, then set your bot token and numeric Telegram user ID:

```env
TELEGRAM_BOT_TOKEN=123456:replace-me
ALLOWED_USER_ID=123456789
```

To find your user ID, message a reputable Telegram ID bot from the account that will use this bot, then place that numeric value in `ALLOWED_USER_ID`. Keep `.env` private.

Start long polling:

```powershell
python -m src.bot
```

## Test

```powershell
pytest
```
