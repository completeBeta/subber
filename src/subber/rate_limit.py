"""Rate limiter for heavy POST endpoints.

File-backed state with fcntl.flock for cross-worker safety (4 uvicorn workers).
5 requests per 60s window per endpoint.
"""

import fcntl
import json
import os
import time as _time
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

_WINDOW_SEC = 60
_MAX_REQUESTS = 5
_LIMITED_ENDPOINTS = {"/api/scan", "/api/translate", "/api/grab", "/api/sync/apply"}
_STATE_FILE = Path(os.environ.get("SUBBER_RATE_LIMIT_FILE", "/tmp/subber_rate_limit.json"))


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Reject POST requests that exceed 5/60s per endpoint."""

    async def dispatch(self, request: Request, call_next):
        if request.method != "POST" or request.url.path not in _LIMITED_ENDPOINTS:
            return await call_next(request)

        now = _time.time()
        rejected = False

        try:
            with open(_STATE_FILE, "a+") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                f.seek(0)
                try:
                    state = json.loads(f.read() or "{}")
                except json.JSONDecodeError:
                    state = {}

                entries = state.get(request.url.path, [])
                cutoff = now - _WINDOW_SEC
                entries = [(ts, ip) for ts, ip in entries if ts > cutoff]

                if len(entries) >= _MAX_REQUESTS:
                    rejected = True
                    state[request.url.path] = entries
                    f.seek(0)
                    f.truncate()
                    json.dump(state, f)
                else:
                    client_ip = request.client.host if request.client else "unknown"
                    entries.append((now, client_ip))
                    state[request.url.path] = entries
                    f.seek(0)
                    f.truncate()
                    json.dump(state, f)

                fcntl.flock(f, fcntl.LOCK_UN)
        except OSError:
            pass

        if rejected:
            return JSONResponse(
                status_code=429,
                content={"error": f"Rate limit exceeded. Max {_MAX_REQUESTS} requests per {_WINDOW_SEC} seconds."},
            )
        return await call_next(request)
