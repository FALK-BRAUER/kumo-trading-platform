"""Telegram delivery, ported from ledger-tool's `tools/scripts/notify-telegram.sh`.

That script has been carrying ledger-tool's job alerts for a while and its behaviours are the reason this
is a port rather than a fresh implementation:

  QUIET HOURS SILENCE, THEY DO NOT DROP. Between 22:00 and 08:00 SGT a normal alert is sent with
  `disable_notification`, so it is waiting in the morning without having buzzed at 3am. An alert
  withheld is an alert lost; an alert delivered silently is just polite.

  CRITICAL IGNORES QUIET HOURS. The engine being down is worth waking up for. ledger-tool draws the same
  line at job failure.

  MARKDOWN IS RETRIED WITHOUT IT. Telegram rejects the whole message if formatting is malformed, and
  the text most likely to break it — a symbol with an underscore, a broker error containing
  backticks — is exactly the text worth delivering. On a 4xx we resend as plain text rather than
  losing the alert to a formatting error.

Secrets come from the environment, never from settings (`config/settings/notifications.schema.json`
holds behaviour only, including WHICH account to send as). The account name maps to env vars by the
convention the other projects already use — `{UPPER_ACCOUNT}_TELEGRAM_BOT_TOKEN`, hyphens stripped —
so the cockpit can send as an existing bot without minting a new one. ledger-tool reads the macOS
keychain directly, which a container cannot, so the deploy exports the pair as the Alpaca keys are:

    export LEDGER_TOOL_TELEGRAM_BOT_TOKEN=$(security find-generic-password -a ledger_tool -s LEDGER_TOOL_TELEGRAM_BOT_TOKEN -w)
    export LEDGER_TOOL_TELEGRAM_CHAT_ID=$(security find-generic-password -a ledger_tool -s LEDGER_TOOL_TELEGRAM_CHAT_ID -w)

Delivery is BEST EFFORT and must never affect trading. Every failure path returns False and logs;
nothing here raises into a caller. A notifier that can break a session is worse than no notifier.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp

_log = logging.getLogger(__name__)

SGT = ZoneInfo("Asia/Singapore")
_API = "https://api.telegram.org/bot{token}/sendMessage"
_TIMEOUT = 10.0
"""Short. A hung notification must not hold up whatever asked for it."""


@dataclass(frozen=True)
class Alert:
    """One thing worth telling the operator about.

    `critical` is not a severity label for its own sake — it is the single decision of whether this
    may wake someone up, so it is a boolean rather than an enum of levels nobody would agree on.
    """

    title: str
    body: str
    critical: bool = False


def env_keys(account: str) -> tuple[str, str]:
    """Account name -> the env var pair holding its credentials.

    Mirrors `~/scaffold-tool/tools/scripts/notify-telegram.sh`: uppercase, hyphens stripped. Keeping the
    same mapping means an account already provisioned for another project works here with no new bot
    and no new keychain entry — 'shared-hub' -> SHAREDHUB_TELEGRAM_BOT_TOKEN.
    """
    upper = account.replace("-", "").upper()
    return f"{upper}_TELEGRAM_BOT_TOKEN", f"{upper}_TELEGRAM_CHAT_ID"


#: The two legacy-Markdown entity openers NO alert author ever means as markup: `_` arrives inside
#: identifiers (`reconcile_drift`, `exec_position_state`) and `[` inside venue text. Telegram rejects
#: the WHOLE message when either opens an entity that never closes — measured on an Alpaca paper instance 2026-09-10:
#: the `_` in `reconcile_drift` at byte 289 of the stranded-claims alert, twice (#860).
#: `*` and the backtick are NOT escaped: nine alert sites bold a symbol or wrap detail in a code
#: span on purpose (alerts.py, pool_sweep.py), and escaping them showed the reader literal
#: punctuation (review). An unpaired one of those still reaches the plain retry below.
_MD_ENTITY_CHARS = "_["


def _escape_md(text: str) -> str:
    return "".join(f"\\{c}" if c in _MD_ENTITY_CHARS else c for c in text)


def _format(alert: Alert, prefix: str, now: datetime) -> str:
    """ledger-tool's shape, so both projects' alerts read alike in the same client."""
    mark = "🔴" if alert.critical else "•"
    stamp = now.astimezone(SGT).strftime("%H:%M SGT · %b %d, %Y")
    return (f"*{prefix}* · {mark} {_escape_md(alert.title)}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{_escape_md(alert.body)}\n\n"
            f"⏰ {stamp}")


class TelegramNotifier:
    """Sends via the Telegram Bot API. Unconfigured is a normal state, not an error.

    The cockpit runs on a laptop, in CI, and in a container that may have no credentials. Treating a
    missing token as a failure would mean either noisy logs everywhere or a notifier nobody dares
    call. `configured` says whether delivery is possible; `send` is a no-op returning False if not.
    """

    def __init__(self, token: str | None = None, chat_id: str | None = None,
                 prefix: str = "kumo-trading-platform", account: str = "ledger_tool") -> None:
        env_token, env_chat = env_keys(account)
        self._token = token if token is not None else os.environ.get(env_token, "")
        self._chat = chat_id if chat_id is not None else os.environ.get(env_chat, "")
        self._prefix = prefix
        self._account = account
        self._warned_unconfigured = False

    @property
    def account(self) -> str:
        return self._account

    @property
    def configured(self) -> bool:
        return bool(self._token and self._chat)

    async def send(self, alert: Alert, *, silent: bool = False) -> bool:
        """True only when Telegram accepted it. Never raises."""
        if not self.configured:
            # WARNING, once per notifier — NOT debug (#425 follow-up, 2026-08-21).
            #
            # This was `_log.debug`, on the reasoning in the class docstring: a laptop or CI run has no
            # credentials and should not shout about it. Correct instinct, wrong level. In the paper
            # deploy the settings said `enabled: true` while every alert was dropped, and at the normal
            # log level a misconfigured stack and a quiet market were byte-identical. Nobody could have
            # noticed, because there was nothing to notice.
            #
            # `_warned_unconfigured` keeps the original intent intact: the alert loop runs per poll, so
            # warning on every drop would re-bury the signal under its own volume. Said once, it is a
            # startup-shaped message that names exactly which two variables are missing.
            if not self._warned_unconfigured:
                self._warned_unconfigured = True
                env_token, env_chat = env_keys(self._account)
                missing = [n for n, v in ((env_token, self._token), (env_chat, self._chat)) if not v]
                _log.warning(
                    "telegram notifier UNCONFIGURED — alerts are enabled but %s %s empty, so every "
                    "alert is being dropped (first dropped: %r). Export the pair before `compose up`; "
                    "compose substitutes a missing var as an empty string.",
                    " and ".join(missing), "is" if len(missing) == 1 else "are", alert.title,
                )
            else:
                _log.debug("telegram notifier unconfigured — dropping %r", alert.title)
            return False

        text = _format(alert, self._prefix, datetime.now(SGT))
        payload = {"chat_id": self._chat, "text": text,
                   "disable_notification": silent, "parse_mode": "Markdown"}
        url = _API.format(token=self._token)
        timeout = aiohttp.ClientTimeout(total=_TIMEOUT)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload) as r:
                    if r.status == 200:
                        return True
                    # Redacted too: an error body from a proxy commonly echoes the request URL, and
                    # the URL carries the token (codex review).
                    status, body = r.status, self._redact((await r.text())[:200])
                # Formatting is the usual cause — an underscore in a symbol, a backtick in a broker
                # error. Resend as plain text rather than lose the alert to its own decoration.
                # The body is logged because Telegram names the offending entity in it, which is the
                # only way to tell a formatting fault from a bad chat id or a revoked token.
                _log.warning("telegram rejected a formatted message (HTTP %s: %s); retrying plain",
                             status, body)
                payload.pop("parse_mode")
                async with session.post(url, json=payload) as r:
                    if r.status == 200:
                        # SAID, NOT SILENT (#860). The plain retry used to return True and log
                        # nothing, so a formatting rejection followed by a lost alert and one
                        # followed by a delivered alert read identically. Telegram's message id is
                        # the proof it landed; WARNING because a rejection after escaping means the
                        # escape missed something and the next reader should know.
                        # THE RECORD MUST NOT COST THE DELIVERY (review): Telegram has already
                        # answered 200, so a body read that fails here is a broken proof, never a
                        # lost alert — reporting False would re-send it on the next poll.
                        try:
                            proof = self._redact((await r.text())[:120])
                        except Exception as exc:                        # noqa: BLE001
                            proof = f"<body unreadable: {type(exc).__name__}>"
                        _log.warning("telegram delivered PLAIN after a formatting rejection: %r "
                                     "(response %s)", alert.title, proof)
                        return True
                    _log.error("telegram delivery failed (HTTP %s): %s", r.status,
                               self._redact((await r.text())[:200]))
                    return False
        except Exception as exc:                                        # noqa: BLE001
            # Best effort by design: DNS, TLS, timeout, a proxy — none of it is worth failing a
            # trading session over, and the caller has no useful recovery anyway.
            #
            # REDACTED, because the bot token is IN THE URL and aiohttp embeds the URL in several of
            # its exceptions (InvalidUrlClientError and ClientResponseError both do — verified). A
            # plain `%r` here would write a live credential into the container logs, where it would
            # then be shipped anywhere logs go.
            _log.error("telegram delivery error: %s", self._redact(repr(exc)))
            return False

    def _redact(self, text: str) -> str:
        """Remove the bot token from anything about to be logged. Never the chat id — that is not a
        credential and knowing which chat failed is the useful part of the message."""
        return text.replace(self._token, "***") if self._token else text
