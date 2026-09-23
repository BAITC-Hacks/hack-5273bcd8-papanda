"""The public catalog never excludes a task because of a low score."""
from app.contracts import Task


def catalog(tasks: list[Task], topic: str | None = None, level: str | None = None) -> list[Task]:
    items = [task for task in tasks if task.status == "published"]
    if topic:
        items = [task for task in items if topic.casefold() in task.industry.casefold()]
    if level:
        items = [task for task in items if task.score.level == level]
    return sorted(items, key=lambda task: (task.score.total, task.published_at or "", task.id), reverse=True)
