from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Task(Base):
    """Main business entity for the ops dashboard."""

    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    sku: Mapped[str] = mapped_column(String(100), default="", index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    delivery_type: Mapped[str] = mapped_column(String(100), default="")
    planned_price: Mapped[str] = mapped_column(String(100), default="")
    comment: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(50), default="new")
    assignee: Mapped[str] = mapped_column(String(120), default="")
    due_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    planned_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    telegram_chat_id: Mapped[str] = mapped_column(String(100), default="")
    reminder_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AuditLog(Base):
    """Simple audit trail for important actions in the tool."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    details: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
