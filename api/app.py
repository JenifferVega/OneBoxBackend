"""Fábrica de la aplicación FastAPI de OneBox."""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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

    for router in all_routers:
        app.include_router(router)

    return app
