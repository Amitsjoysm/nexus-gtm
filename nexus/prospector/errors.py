"""The one exception Prospector's services raise, carrying the HTTP status it means.

Services decide what went wrong and say it in a sentence for the person who asked; the router only
translates. Keeping the status beside the message stops each endpoint re-deciding whether "the data
provider did not answer" is a 503 or a 504.
"""
from __future__ import annotations


class ProspectorError(Exception):
    def __init__(self, status: int, detail: str, *, headers: dict[str, str] | None = None):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.headers = headers or {}
