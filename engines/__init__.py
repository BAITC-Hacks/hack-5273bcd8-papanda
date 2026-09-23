"""Stable in-process entry point. Engines are configured once per process."""
import os
from typing import Protocol
from pathlib import Path

from dotenv import load_dotenv

from .common.contracts import Answer, RunView, StartRequest


class Engine(Protocol):
    name: str

    async def start(self, req: StartRequest) -> str: ...
    def view(self, run_id: str) -> RunView: ...
    async def submit_answers(self, run_id: str, answers: list[Answer]) -> None: ...
    async def cancel(self, run_id: str) -> None: ...
    def trace(self, run_id: str) -> list[dict]: ...


_instances: dict[str, Engine] = {}


def get_engine(name: str | None = None) -> Engine:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    chosen = name or os.getenv("ENGINE", "light")
    if chosen not in {"light", "heavy"}:
        raise ValueError("ENGINE должен быть light или heavy")
    if chosen not in _instances:
        if chosen == "light":
            from .light.engine import LightEngine
            _instances[chosen] = LightEngine()
        else:
            from .heavy.engine import HeavyEngine
            _instances[chosen] = HeavyEngine()
    return _instances[chosen]
