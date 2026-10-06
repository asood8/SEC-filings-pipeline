"""The summary shape every summarizer produces, whether a template or an LLM writes it."""

from pydantic import BaseModel, Field


class FilingSummary(BaseModel):
    headline: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1)
    key_points: list[str] = Field(default_factory=list, max_length=8)
