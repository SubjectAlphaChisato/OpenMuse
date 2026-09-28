from __future__ import annotations


class ApiError(Exception):
    status = 400
    code = "bad_request"

    def __init__(self, message: str | None = None, *, code: str | None = None, status: int | None = None):
        super().__init__(message or self.code)
        self.message = message or self.code
        if code:
            self.code = code
        if status:
            self.status = status


class Unauthorized(ApiError):
    status = 401
    code = "unauthenticated"


class Forbidden(ApiError):
    status = 403
    code = "forbidden"


class NotFound(ApiError):
    status = 404
    code = "not_found"


class Conflict(ApiError):
    status = 409
    code = "conflict"


class Invalid(ApiError):
    status = 422
    code = "invalid_input"


class Throttled(ApiError):
    status = 429
    code = "rate_limited"
