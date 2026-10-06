from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DiscoverySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DISCOVERY_", env_file=".env", extra="ignore")

    watchlist_path: Path = Path("config/watchlist.toml")
    # Only filings this recent are fetched, which bounds the first run for a new company.
    lookback_days: int = Field(default=30, ge=1)
    # EDGAR form codes to collect (env: JSON list, e.g. DISCOVERY_FORM_TYPES='["4","8-K"]').
    form_types: frozenset[str] = frozenset({"4", "8-K"})
