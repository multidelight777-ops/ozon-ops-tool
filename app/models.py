from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
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


class Review(Base):
    """Ozon review/question entity with moderation and reply workflow fields."""

    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    external_id: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    sku: Mapped[str] = mapped_column(String(100), default="", index=True)
    product_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_type: Mapped[str] = mapped_column(String(50), default="review")
    category: Mapped[str | None] = mapped_column(String(50), nullable=True)
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    author_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    text: Mapped[str] = mapped_column(Text, default="")
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="new")
    draft_reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    automation_mode: Mapped[str] = mapped_column(String(50), default="review_required")
    processing_mode: Mapped[str] = mapped_column(String(50), default="требуется_проверка")
    confidence_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk_level: Mapped[str] = mapped_column(String(50), default="medium")
    send_status: Mapped[str] = mapped_column(String(50), default="pending")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_automation_processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    high_risk_notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class DiscountRequest(Base):
    """Заявка на скидку Ozon для внутренней панели и будущей интеграции."""

    __tablename__ = "discount_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    external_id: Mapped[str] = mapped_column(String(100), unique=True, index=True, default="")
    sku: Mapped[str] = mapped_column(String(100), index=True, default="")
    product_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    current_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    requested_discount_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    requested_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    approved_discount_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    approved_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    buyer_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="новая")
    send_status: Mapped[str] = mapped_column(String(50), default="ожидает")
    ozon_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PriceMonitor(Base):
    """Результат проверки витринной цены товара."""

    __tablename__ = "price_monitors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    sku: Mapped[str] = mapped_column(String(100), default="", index=True)
    url: Mapped[str] = mapped_column(String(500), default="")
    price_with_spp: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_without_spp: Mapped[float | None] = mapped_column(Float, nullable=True)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MonitoredProduct(Base):
    """Товар, который мы отслеживаем на витрине Ozon."""

    __tablename__ = "monitored_products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    sku: Mapped[str] = mapped_column(String(100), default="", index=True)
    product_name: Mapped[str] = mapped_column(String(255), default="")
    url: Mapped[str] = mapped_column(String(500), default="")
    base_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_with_spp: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_without_spp: Mapped[float | None] = mapped_column(Float, nullable=True)
    percent_with_spp: Mapped[float | None] = mapped_column(Float, nullable=True)
    percent_without_spp: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_checked: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
