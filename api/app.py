"""OneBox FastAPI application factory."""
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from agent import obs

ALLOWED_ORIGINS = [
    "http://localhost:5173",
    "http://localhost:3000",
    "https://www.oneboxmanager.com",
    "https://oneboxmanager.com",
    "https://d1mft4quq3ui5e.cloudfront.net",
]


def create_app() -> FastAPI:
    from api.controllers import all_routers

    app = FastAPI(title="OneBox Agent", version="1.0.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def _observe(request: Request, call_next):
        """Give every line logged during one request the same id.

        Without this, the log of a busy moment is several users' lines
        interleaved with no way to tell them apart. With it, one query
        (`filter request_id = "..."`) returns exactly one request.

        The uid is NOT set here: the endpoint sets it after validating auth,
        and obs reads it from the contextvar, so lines logged before that
        simply have no uid -- which is the truth.
        """
        rid = request.headers.get("x-request-id") or obs.new_request_id()
        obs.bind(request_id=rid)
        started = time.monotonic()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["x-request-id"] = rid
            return response
        except Exception as e:
            obs.error("request_crashed", exc=e,
                      method=request.method, path=request.url.path)
            raise
        finally:
            ms = round((time.monotonic() - started) * 1000)
            # Health checks and the notification poll would otherwise be most
            # of the log volume, and nobody ever reads them.
            noisy = request.url.path in ("/health", "/api/notifications")
            if not noisy or status >= 400:
                obs.log("request", method=request.method,
                        path=request.url.path, status=status, duration_ms=ms)
            obs.clear()

    for router in all_routers:
        app.include_router(router)

    return app
