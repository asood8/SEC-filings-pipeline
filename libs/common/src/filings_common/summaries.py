"""The summary shape every summarizer produces, whether a template or an LLM writes it."""

from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field


def _one_line(text: str) -> str:
    """Runs of whitespace, line breaks included, become one space. An LLM can put a line break
    in a headline, and a headline becomes an email's Subject header, which can't hold one."""
    line = " ".join(text.split())
    if not line:
        raise ValueError("must not be blank")
    return line


OneLine = Annotated[str, AfterValidator(_one_line)]

# Shown with every summary that leaves the website: alerts and the Atom feed.
DISCLAIMER = "Automated summary; may contain errors and is not investment advice."


class FilingSummary(BaseModel):
    headline: OneLine = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1)
    key_points: list[OneLine] = Field(default_factory=list, max_length=8)
