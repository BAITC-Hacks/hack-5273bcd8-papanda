"""A tiny terminal assistant for demonstrating a GitHub project workflow."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path


TASKS_FILE = Path("tasks.txt")


def load_tasks() -> list[str]:
    """Return saved tasks, if any."""
    if not TASKS_FILE.exists():
        return []
    return [line.strip() for line in TASKS_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]


def save_task(task: str) -> str:
    """Append one task to the local task list."""
    with TASKS_FILE.open("a", encoding="utf-8") as tasks_file:
        tasks_file.write(f"{task}\n")
    return f"Задача сохранена: {task}"


def reply(message: str) -> str:
    """Choose an answer or an action based on a simple command."""
    text = message.strip()
    command = text.lower()

    if command in {"привет", "hello", "hi"}:
        return "Привет! Я мини-агент. Напишите 'помощь', чтобы увидеть команды."
    if command in {"помощь", "help"}:
        return "Команды: 'время', 'задача <текст>', 'задачи', 'выход'."
    if command in {"время", "time"}:
        return f"Сейчас: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    if command.startswith("задача "):
        task = text[7:].strip()
        return save_task(task) if task else "После слова 'задача' добавьте её описание."
    if command in {"задачи", "tasks"}:
        tasks = load_tasks()
        return "Список пуст." if not tasks else "Задачи:\n" + "\n".join(f"{index}. {task}" for index, task in enumerate(tasks, 1))
    return "Я пока не знаю эту команду. Напишите 'помощь'."


def main() -> None:
    print("Мини-агент запущен. Для выхода напишите 'выход'.")
    while True:
        message = input("> ")
        if message.strip().lower() in {"выход", "exit", "quit"}:
            print("До встречи!")
            break
        print(reply(message))


if __name__ == "__main__":
    main()
