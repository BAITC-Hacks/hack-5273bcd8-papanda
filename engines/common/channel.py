"""Per-run answer rendezvous, separate from short model deadlines."""
import asyncio

from .contracts import Answer, Question


class AnswerChannel:
    def __init__(self, timeout_s: float = 900):
        self.timeout_s = timeout_s
        self._waiting: dict[str, asyncio.Future[list[Answer]]] = {}

    async def ask(self, run_id: str, questions: list[Question]) -> list[Answer]:
        if run_id in self._waiting:
            raise RuntimeError("Прогон уже ожидает ответы")
        future = asyncio.get_running_loop().create_future()
        self._waiting[run_id] = future
        try:
            return await asyncio.wait_for(future, self.timeout_s)
        finally:
            self._waiting.pop(run_id, None)

    def submit(self, run_id: str, answers: list[Answer]) -> None:
        future = self._waiting.get(run_id)
        if future is None or future.done():
            raise ValueError("Прогон не ожидает ответы")
        future.set_result(answers)

    def cancel(self, run_id: str) -> None:
        future = self._waiting.get(run_id)
        if future is not None and not future.done():
            future.cancel()
