from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DiscoverySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DISCOVERY_", env_file=".env", extra="ignore")

    watchlist_path: Path = Path("config/watchlist.toml")
    # Only filings this recent are fetched, which bounds the first run for a new company.
    lookback_days: int = Field(default=30, ge=1)
    # EDGAR form codes to collect (env: JSON list, e.g. DISCOVERY_FORM_TYPES='["4","8-K"]').
    # "/A" is an amendment, collected like any filing and linked to the one it amends.
    form_types: frozenset[str] = frozenset({"4", "4/A", "8-K", "8-K/A"})
    # Forms whose EX-99 exhibits (press releases) are downloaded too.
    exhibit_form_types: frozenset[str] = frozenset({"8-K", "8-K/A"})

    # Earnings-call transcripts from the companies' own sites (ADR-0022, `fetch-transcripts`).
    transcript_sources_path: Path = Path("config/transcript_sources.toml")
    # Transcripts of calls this recent are fetched; older ones never are.
    transcript_lookback_days: int = Field(default=120, ge=1)
    # An earnings 8-K this old with no transcript yet counts as overdue (a source that broke).
    transcript_overdue_days: int = Field(default=21, ge=1)
    # Sent to companies' sites (not SEC's): names the project, with no contact address.
    transcript_user_agent: str = (
        "filings-pipeline/1.0 (+https://github.com/asood8/SEC-filings-pipeline)"
    )
