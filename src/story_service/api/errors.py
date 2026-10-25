"""The API's error type. Every error leaves the API as `{"error": {"code", "message", "details"}}` (Section 3)."""

from __future__ import annotations


class ApiError(Exception):
    """An error with an HTTP status and a stable code (`STORY_BUSY`, `NOT_FOUND`, …)."""

    def __init__(
        self, status: int, code: str, message: str, details: dict | None = None, *, headers: dict | None = None
    ):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details or {}
        self.headers = headers or {}  # e.g. Retry-After on 429

    def body(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}


def not_found(what: str) -> ApiError:
    return ApiError(404, "NOT_FOUND", f"{what} not found")
