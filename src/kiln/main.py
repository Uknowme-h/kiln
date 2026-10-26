"""FastAPI app: health, model routing, and the usage counter."""

import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from kiln import __version__
from kiln.agents.roles import Role
from kiln.config import Settings, load_settings
from kiln.events.log import EventLog
from kiln.gateway.budget import BudgetGuard
from kiln.gateway.routing import route_for
from kiln.gateway.usage import UsageLog

logger = logging.getLogger("kiln")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if not settings.model_api_key:
            logger.warning("MODEL_API_KEY is not set. Copy .env.example to .env and add the key.")
        yield

    app = FastAPI(title="Kiln", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.events = EventLog()
    app.state.budget = BudgetGuard(settings.project_token_ceiling)
    app.state.usage = UsageLog()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "kiln", "version": __version__}

    @app.get("/routing")
    def routing() -> dict[str, dict]:
        return {role.value: route_for(role, settings).public() for role in Role}

    @app.get("/usage")
    def usage() -> dict:
        summary = app.state.usage.summary()
        summary["budget"] = app.state.budget.snapshot()
        return summary

    return app


def main() -> None:
    uvicorn.run("kiln.main:create_app", host="127.0.0.1", port=8000, factory=True)
