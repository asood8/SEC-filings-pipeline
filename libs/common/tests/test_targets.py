import pytest

from filings_common.db import Channel
from filings_common.targets import masked_target, target_is_valid

DISCORD = "https://discord.com/api/webhooks/123456/abc-DEF_789"
SLACK = "https://hooks.slack.com/services/T000/B000/XXXXyyyy1234"


@pytest.mark.parametrize(
    ("channel", "target", "valid"),
    [
        (Channel.DISCORD, DISCORD, True),
        (Channel.DISCORD, "https://discordapp.com/api/webhooks/1/x", True),
        (Channel.DISCORD, "https://discord.com.evil.example/api/webhooks/1/x", False),
        (Channel.DISCORD, "http://discord.com/api/webhooks/1/x", False),  # not https
        (Channel.DISCORD, "https://169.254.169.254/latest/meta-data", False),
        (Channel.SLACK, SLACK, True),
        (Channel.SLACK, "https://hooks.slack.com.evil.example/services/T/B/x", False),
        (Channel.EMAIL, "ops@example.com", True),
        (Channel.EMAIL, "ops@example.com\nBcc: victim@example.com", False),  # header injection
        (Channel.EMAIL, "not-an-email", False),
        (Channel.DISCORD, f"{DISCORD}\n", False),  # nothing may trail the URL
        (Channel.SLACK, f"{SLACK}\n", False),
        (Channel.EMAIL, "ops@example.com\n", False),
        (Channel.DISCORD, "https://discord.com/api/webhooks/1/\u0430bc", False),  # Cyrillic
    ],
)
def test_targets_are_validated_strictly(channel: Channel, target: str, valid: bool) -> None:
    assert target_is_valid(channel, target) is valid


@pytest.mark.parametrize(
    ("channel", "target", "masked"),
    [
        (
            Channel.DISCORD,
            "https://discord.com/api/webhooks/123456/abcdefghijklmnop-QRST_uvwxyz",
            "https://discord.com/api/webhooks/123456/…wxyz",
        ),
        (
            Channel.SLACK,
            "https://hooks.slack.com/services/T000/B000/abcdefghijklmnopqrstuvwx",
            "https://hooks.slack.com/services/T000/B000/…uvwx",
        ),
        (Channel.DISCORD, DISCORD, "https://discord.com/api/webhooks/123456/…"),  # short token
        (Channel.EMAIL, "ops@example.com", "ops@example.com"),
    ],
)
def test_webhook_tokens_are_masked(channel: Channel, target: str, masked: str) -> None:
    assert masked_target(channel, target) == masked
