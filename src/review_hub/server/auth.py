"""Access-token middleware: the seam between localhost and everything else.

Local-first posture (locked decision): by default the server binds
127.0.0.1 with no login - a single operator's machine IS the boundary. This
middleware is the clean seam for the day that changes: set
``REVIEW_HUB_ACCESS_TOKEN`` in the environment and every request must
present the same value in ``X-Access-Token`` (or an ``Authorization:
Bearer`` header). Comparison is constant-time. No user registry, no
sessions - a single shared token matches the single-operator threat model.
"""

from __future__ import annotations

import hmac
import os
from typing import Any, Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

ACCESS_TOKEN_ENV = "REVIEW_HUB_ACCESS_TOKEN"
_TOKEN_HEADER = "x-access-token"


def configured_token() -> str:
    """The token required of every request when one is configured."""
    return os.environ.get(ACCESS_TOKEN_ENV, "")


class AccessTokenMiddleware(BaseHTTPMiddleware):
    """Deny requests that do not present the configured token, if any."""

    def __init__(self, app: Any, token: str) -> None:
        super().__init__(app)
        self._token = token

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not self._token:
            return await call_next(request)  # localhost mode: no auth
        supplied = request.headers.get(_TOKEN_HEADER, "")
        if supplied.startswith("Bearer "):
            supplied = supplied[len("Bearer ") :]
        if not supplied or not hmac.compare_digest(supplied, self._token):
            return JSONResponse(
                {"detail": f"missing or invalid {_TOKEN_HEADER} header"}, status_code=401
            )
        return await call_next(request)
