"""Alert targets (webhook URLs and email addresses): validation and display.

Targets come from users, so they are validated strictly, by the API when a subscription is saved
and by the notifier again before every delivery. A webhook URL must be an official Discord or Slack
webhook over https (nothing else is ever requested, so user input can't point the notifier at
internal addresses), and an email address can't carry extra headers.
"""

import re

from filings_common.db import Channel

# Matched against the whole string (fullmatch), so nothing can trail a valid URL, not even a
# newline. ASCII only: no look-alike Unicode characters.
_DISCORD_WEBHOOK = re.compile(
    r"https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+", re.ASCII
)
_SLACK_WEBHOOK = re.compile(r"https://hooks\.slack\.com/services/[A-Z0-9]+/[A-Z0-9]+/[A-Za-z0-9]+")
_EMAIL = re.compile(r"[^@\s<>,;]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", re.ASCII)


def target_is_valid(channel: Channel, target: str) -> bool:
    pattern = {
        Channel.DISCORD: _DISCORD_WEBHOOK,
        Channel.SLACK: _SLACK_WEBHOOK,
        Channel.EMAIL: _EMAIL,
    }
    return pattern[channel].fullmatch(target) is not None


def masked_target(channel: Channel, target: str) -> str:
    """A target that is safe to show or log.

    A webhook URL's last path segment is its secret token (anyone with the full URL can post to the
    channel), so it becomes "…" plus its last 4 characters; a short token is hidden completely.
    Email addresses are shown in full.
    """
    if channel is Channel.EMAIL:
        return target
    base, _, token = target.rpartition("/")
    return f"{base}/…{token[-4:] if len(token) >= 16 else ''}"
