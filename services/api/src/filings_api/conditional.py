"""Conditional GET for the feeds (RFC 9110, 13.1.2): readers poll them, and most polls find
nothing new, so a reader that sends back the ETag it got gets 304 Not Modified, with no body."""

import hashlib

from fastapi import Request, Response


def _matches(if_none_match: str | None, etag: str) -> bool:
    if if_none_match is None:
        return False
    # Weak comparison, as RFC 9110 prescribes for If-None-Match: W/"x" matches "x".
    tags = {tag.strip().removeprefix("W/") for tag in if_none_match.split(",")}
    return "*" in tags or etag in tags


def conditional_response(
    request: Request,
    body: bytes,
    *,
    media_type: str,
    version: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> Response:
    """The body with its ETag, or 304 when the request's If-None-Match already has it.

    `version` is what the ETag is computed from when the body holds something that changes on
    every request without changing the content (the calendar's DTSTAMP); the body by default.
    """
    etag = f'"{hashlib.sha256(body if version is None else version).hexdigest()[:32]}"'
    sent = {"ETag": etag, **(headers or {})}
    if _matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=sent)
    return Response(content=body, media_type=media_type, headers=sent)
