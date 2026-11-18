"""Request ID middleware — enterprise tracing standard.

Every response carries X-Request-ID (echoed from the client or generated),
so support tickets, server logs, and client errors correlate to one id.
"""

import uuid

from starlette.middleware.base import BaseHTTPMiddleware

from backend.config.settings import settings


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        # cap length to prevent header abuse
        request_id = request_id[:128]
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response
