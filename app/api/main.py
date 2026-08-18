"""Owli M0 的单进程 FastAPI 入口。"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI

from app.adapters.selfcheck import SchemaCheckError, initialize_and_check


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_PATH = ROOT / "var" / "owli.db"
DEFAULT_SCHEMA_PATH = ROOT / "app" / "store" / "schema.sql"


def create_app(
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    schema_path: str | Path = DEFAULT_SCHEMA_PATH,
) -> FastAPI:
    database = Path(database_path)
    schema = Path(schema_path)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        try:
            application.state.schema_check = initialize_and_check(database, schema)
        except SchemaCheckError as error:
            print(str(error), file=sys.stderr)
            raise
        yield

    application = FastAPI(title="Owli", lifespan=lifespan)

    @application.get("/api/health")
    async def health() -> dict:
        return {
            "ok": True,
            "data": {"schema": application.state.schema_check},
            "error": None,
        }

    return application


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8721, workers=1)
