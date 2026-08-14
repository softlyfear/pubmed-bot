"""ORM-модели SQLite по схеме PROJECT.md, раздел 6."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Базовый класс декларативных моделей."""


class User(Base):
    """Пользователь Telegram; whitelist нет, пишем всех."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    searches: Mapped[list[Search]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    favorites: Mapped[list[Favorite]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    notes: Mapped[list[Note]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    subscriptions: Mapped[list[Subscription]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )


class Search(Base):
    """Ровно один текущий поиск на пользователя."""

    __tablename__ = "searches"
    __table_args__ = (
        CheckConstraint("length(query_text) > 0", name="ck_searches_query_text"),
        CheckConstraint("length(query_en) > 0", name="ck_searches_query_en"),
        CheckConstraint("page >= 1", name="ck_searches_page"),
        CheckConstraint("ncbi_retstart >= 0", name="ck_searches_ncbi_retstart"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    query_text: Mapped[str] = mapped_column(Text)
    query_en: Mapped[str] = mapped_column(Text)
    page: Mapped[int] = mapped_column(Integer)
    ncbi_retstart: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    list_chat_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    list_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    user: Mapped[User] = relationship(back_populates="searches")
    results: Mapped[list[SearchResult]] = relationship(
        back_populates="search",
        cascade="all, delete-orphan",
    )


class SearchResult(Base):
    """Строка текущей страницы выдачи."""

    __tablename__ = "search_results"
    __table_args__ = (
        UniqueConstraint("search_id", "position", name="uq_search_results_position"),
        UniqueConstraint("search_id", "pmid", name="uq_search_results_pmid"),
        CheckConstraint(
            "position BETWEEN 1 AND 10",
            name="ck_search_results_position",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    search_id: Mapped[int] = mapped_column(
        ForeignKey("searches.id", ondelete="CASCADE"),
        index=True,
    )
    position: Mapped[int] = mapped_column(Integer)
    pmid: Mapped[str] = mapped_column(Text, index=True)
    title_en: Mapped[str] = mapped_column(Text)
    title_ru: Mapped[str | None] = mapped_column(Text, nullable=True)
    date_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    status_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    integrity_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    pub_type_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    viewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    search: Mapped[Search] = relationship(back_populates="results")


class Favorite(Base):
    """Избранная статья пользователя."""

    __tablename__ = "favorites"
    __table_args__ = (UniqueConstraint("user_id", "pmid", name="uq_favorites_user_pmid"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    pmid: Mapped[str] = mapped_column(Text)
    title_en: Mapped[str] = mapped_column(Text)
    title_ru: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    user: Mapped[User] = relationship(back_populates="favorites")


class Note(Base):
    """Заметка к PMID, лимит 2000 символов."""

    __tablename__ = "notes"
    __table_args__ = (
        UniqueConstraint("user_id", "pmid", name="uq_notes_user_pmid"),
        CheckConstraint("length(body) <= 2000", name="ck_notes_body_len"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    pmid: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    title_en: Mapped[str | None] = mapped_column(Text, nullable=True)
    title_ru: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    user: Mapped[User] = relationship(back_populates="notes")


class Subscription(Base):
    """Подписка на новые статьи по запросу."""

    __tablename__ = "subscriptions"
    __table_args__ = (
        CheckConstraint("length(query_en) > 0", name="ck_subscriptions_query_en"),
        UniqueConstraint("user_id", "query_text", name="uq_subscriptions_user_query"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    query_text: Mapped[str] = mapped_column(Text)
    query_en: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="1",
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    user: Mapped[User] = relationship(back_populates="subscriptions")
    deliveries: Mapped[list[SubscriptionDelivery]] = relationship(
        back_populates="subscription",
        cascade="all, delete-orphan",
    )


class SubscriptionDelivery(Base):
    """Уже доставленный PMID по подписке (идемпотентность)."""

    __tablename__ = "subscription_deliveries"
    __table_args__ = (
        UniqueConstraint("subscription_id", "pmid", name="uq_subscription_deliveries_pmid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"),
    )
    pmid: Mapped[str] = mapped_column(Text)
    delivered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )

    subscription: Mapped[Subscription] = relationship(back_populates="deliveries")


class QueryTranslationCache(Base):
    """Кэш RU→EN запросов: хэш исходника → английский term."""

    __tablename__ = "query_translation_cache"
    __table_args__ = (
        UniqueConstraint("source_hash", name="uq_query_translation_cache_hash"),
        CheckConstraint("length(text_en) > 0", name="ck_query_translation_cache_text_en"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_hash: Mapped[str] = mapped_column(Text)
    text_en: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )


class TranslationCache(Base):
    """Кэш перевода: pmid + kind + хэш исходника."""

    __tablename__ = "translation_cache"
    __table_args__ = (
        UniqueConstraint("pmid", "kind", "source_hash", name="uq_translation_cache_key"),
        CheckConstraint(
            "kind IN ('title', 'abstract', 'fulltext')",
            name="ck_translation_cache_kind",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pmid: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    source_hash: Mapped[str] = mapped_column(Text)
    text_ru: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )


class AuditLog(Base):
    """Служебный лог без тел статей; записи старше 31 дня удаляет джоб."""

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_telegram_user_id", "telegram_user_id"),
        Index("ix_audit_logs_event", "event"),
        Index("ix_audit_logs_created_at", "created_at"),
        CheckConstraint(
            "event IN ('search', 'open', 'error', 'ncbi', 'translate', 'subscription')",
            name="ck_audit_logs_event",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    event: Mapped[str] = mapped_column(Text)
    query_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    pmid: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
