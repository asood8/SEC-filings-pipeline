"""Request and response models. They define the API contract and its OpenAPI documentation."""

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from filings_common.db import Channel, FilingStatus

# The forms a subscription can choose to be alerted about: the ones the pipeline parses and
# summarizes. A form the pipeline learns later must be added here before anyone can pick it.
AlertFormType = Literal["4", "8-K"]


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None = Field(
        description="Pass as `cursor` to get the next page; null on the last page."
    )


class FilingOut(BaseModel):
    accession_no: str
    cik: int
    ticker: str
    company_name: str
    form_type: str
    filed_at: date
    accepted_at: datetime | None
    status: FilingStatus
    headline: str | None = Field(description="The current summary's headline, once summarized.")
    sec_url: str = Field(description="The filing's primary document on sec.gov.")


class SummaryOut(BaseModel):
    headline: str
    summary: str
    key_points: list[str]
    provider: str = Field(description='"template" for Form 4s; the LLM provider for 8-Ks.')
    model: str
    prompt_version: int
    created_at: datetime
    ungrounded_numbers: list[str] | None = Field(
        description=(
            "Numbers in an LLM summary that don't appear in the filing text, worth verifying. "
            "Empty when all were found; null when not checked (template summaries)."
        )
    )


class Form4Details(BaseModel):
    period_of_report: date
    issuer_cik: int = Field(description="Whose stock was traded; may differ from `cik`.")
    issuer_name: str
    issuer_ticker: str | None
    reporting_owners: list[str]
    transaction_codes: list[str]
    is_10b5_1: bool | None


class ExhibitOut(BaseModel):
    type: str
    filename: str
    sec_url: str


class FilingDetail(FilingOut):
    summary: SummaryOut | None
    form4: Form4Details | None
    items: list[str] | None = Field(description="8-K item numbers, e.g. 2.02 and 9.01.")
    exhibits: list[ExhibitOut]


class CompanyOut(BaseModel):
    cik: int
    ticker: str
    name: str
    exchange: str | None
    on_watchlist: bool


class CompanyDetail(CompanyOut):
    recent_filings: list[FilingOut]


class SearchResultOut(FilingOut):
    score: float = Field(description="Higher is better; comparable within one response only.")


class SearchResponse(BaseModel):
    query: str
    mode: Literal["keyword", "semantic", "hybrid"] = Field(description="The mode actually used.")
    degraded: bool = Field(
        description="True when semantic or hybrid search was requested but fell back to keywords."
    )
    items: list[SearchResultOut]


class GoogleSignIn(BaseModel):
    credential: str = Field(
        min_length=1,
        max_length=8192,
        description="The ID token (a JWT) that Google's Sign in with Google button returns.",
    )


class UserOut(BaseModel):
    id: int
    email: str
    created_at: datetime


def _sorted_unique(forms: list[AlertFormType]) -> list[AlertFormType]:
    return sorted(set(forms))


FormTypes = Annotated[list[AlertFormType], AfterValidator(_sorted_unique)]
_FORM_TYPES_HELP = "Alert only about these forms; empty means every form."
_TARGET_HELP = (
    "A Discord or Slack webhook URL. For email, your account's address: the default, and the only "
    "one allowed."
)


class SubscriptionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: str = Field(min_length=1, max_length=20, examples=["AAPL"])
    form_types: FormTypes = Field(default_factory=list, description=_FORM_TYPES_HELP)
    channel: Channel
    target: str | None = Field(default=None, max_length=500, description=_TARGET_HELP)


class SubscriptionUpdate(BaseModel):
    """Omitted fields keep their current values."""

    model_config = ConfigDict(extra="forbid")

    form_types: FormTypes | None = Field(default=None, description=_FORM_TYPES_HELP)
    channel: Channel | None = Field(
        default=None,
        description="A new channel needs a new `target`, except email, which uses your address.",
    )
    target: str | None = Field(default=None, max_length=500, description=_TARGET_HELP)


class CompanyRef(BaseModel):
    cik: int
    ticker: str
    name: str


class DeliveryOut(BaseModel):
    status: Literal["pending", "sent", "failed"] = Field(
        description="pending: not delivered yet and will be retried; failed: rejected for good."
    )
    accession_no: str = Field(description="The filing the alert was about.")
    at: datetime = Field(description="When it was sent, or last attempted.")
    error: str | None = Field(
        description="Why the last attempt failed, e.g. a deleted webhook. Null once sent."
    )


class SubscriptionOut(BaseModel):
    id: int
    company: CompanyRef
    form_types: list[str] = Field(description="Empty means every form.")
    channel: Channel
    target: str = Field(
        description=(
            "Webhook URLs are masked: they contain a secret token, shown only by its last 4 "
            "characters. Email addresses are shown in full."
        )
    )
    created_at: datetime
    last_delivery: DeliveryOut | None = Field(
        default=None, description="The most recent alert for this subscription, if any."
    )


class Destination(BaseModel):
    """Where alerts go. For a webhook, give its URL as `target`, or `target_from` to reuse the
    webhook of one of your existing subscriptions without pasting it again."""

    model_config = ConfigDict(extra="forbid")

    channel: Channel
    target: str | None = Field(default=None, max_length=500, description=_TARGET_HELP)
    target_from: int | None = Field(
        default=None,
        ge=1,
        le=2**63 - 1,
        description="The id of one of your subscriptions on the same channel, to copy its target.",
    )


class SubscriptionBatch(BaseModel):
    """Every combination of `tickers` and `destinations`, with the same form types."""

    model_config = ConfigDict(extra="forbid")

    tickers: list[Annotated[str, Field(min_length=1, max_length=20)]] = Field(
        min_length=1, max_length=25, examples=[["AAPL", "MSFT"]]
    )
    form_types: FormTypes = Field(default_factory=list, description=_FORM_TYPES_HELP)
    destinations: list[Destination] = Field(min_length=1, max_length=5)


class SubscriptionBatchOut(BaseModel):
    created: list[SubscriptionOut]
    existing: list[SubscriptionOut] = Field(
        description="Combinations you were already subscribed to, left unchanged."
    )
