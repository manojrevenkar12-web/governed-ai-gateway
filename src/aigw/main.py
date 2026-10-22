"""ASGI entry point: `uvicorn aigw.main:app`. Configured by environment variables (see README)."""

from __future__ import annotations

import os

from aigw.app import Settings, create_app

settings = Settings.from_env()
settings.budget_check_then_spend = os.environ.get("AIGW_BUDGET_CHECK_THEN_SPEND") == "1"
app = create_app(settings)
