from datetime import datetime

from pydantic import BaseModel, Field


class TaskCreate(BaseModel):
    """Validation for manual task creation."""

    title: str = Field(..., min_length=1, max_length=200)
    description: str = ""
    assignee: str = ""
    due_date: datetime | None = None
    telegram_chat_id: str = ""
