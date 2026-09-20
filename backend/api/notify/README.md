# api/notify

Operator alerts over Telegram (#197). Ported from ledger-tool's `tools/scripts/notify-telegram.sh`, so
both projects' messages read alike in the same client.

- `telegram.py` — `TelegramNotifier` (delivery, quiet-hours helper, message shape) and `Alert`.
  Uses `aiohttp`, already a backend dependency; adding a new HTTP client would also need adding to
  `deploy/Dockerfile.backend`'s pip list or the container crash-loops.
- `__init__.py` — `Notifier`: reads the `notifications` settings domain, applies quiet hours, and
  deduplicates a repeating condition so an alert means "this started".

What goes here: alert transports and the policy over them. What doesn't: the schema
(`backend/config/settings/notifications.schema.json`), the values (the settings volume), or secrets.

**Which bot is a setting; its credentials are not.** `telegram_account` (default `ledger_tool`) maps to
`{UPPER}_TELEGRAM_BOT_TOKEN` / `_CHAT_ID` with hyphens stripped — the same convention as
`~/scaffold-tool/tools/scripts/notify-telegram.sh`, so any account already provisioned for another project
works here with no new bot and no new keychain entry.

Default is `ledger_tool`: the cockpit's alerts are trading alerts and belong beside ledger-tool's rather
than in a second channel to remember to check.

Secrets stay environment-only. ledger-tool reads the macOS keychain directly; a container cannot, so the
deploy exports the pair as the Alpaca keys are exported:

```bash
export LEDGER_TOOL_TELEGRAM_BOT_TOKEN=$(security find-generic-password -a ledger_tool -s LEDGER_TOOL_TELEGRAM_BOT_TOKEN -w)
export LEDGER_TOOL_TELEGRAM_CHAT_ID=$(security find-generic-password -a ledger_tool -s LEDGER_TOOL_TELEGRAM_CHAT_ID -w)
docker compose -f deploy/compose.paper.yml -p kumo-paper up -d
```

To move to a dedicated bot later: create it with BotFather, store the pair under keychain account
`kumo`, export `KUMO_TELEGRAM_*` (compose already passes both pairs), and set `telegram_account` to
`kumo` in the UI. No code change and no redeploy — the transport is resolved per send.

Unconfigured is a normal state: `configured` is False, `send` is a no-op, nothing logs at error.
Delivery is best effort throughout — nothing here raises into a trading path.
