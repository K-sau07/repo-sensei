"""Ask's conversations (4.7): a user's questions about one repository and the answers, each
answer's parts as the client renders them (text, tool calls, citations, confidence) and its
citations, one row each, at the commit they cite."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sensei_core.db.base import Base, IdMixin, TimestampMixin
from sensei_core.db.tenancy import OrgScopedMixin
from sensei_core.db.types import HalfVector
from sensei_core.models.chunks import EMBEDDING_DIMENSIONS


class Conversation(IdMixin, OrgScopedMixin, TimestampMixin, Base):
    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("id", "org_id"),  # for the (conversation, org) foreign key of messages
        Index("ix_conversations_repo_user_updated", "repo_id", "user_id", "updated_at"),
        # With org_id: a conversation can't be about another org's repository.
        ForeignKeyConstraint(
            ["repo_id", "org_id"], ["repos.id", "repos.org_id"], ondelete="CASCADE"
        ),
    )

    repo_id: Mapped[uuid.UUID] = mapped_column()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(Text, default="", server_default="")
    # The commit it was first asked at (later questions may be asked at a newer one).
    snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("snapshots.id", ondelete="SET NULL"), index=True
    )
    share_token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)


class ConversationMessage(IdMixin, OrgScopedMixin, Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("id", "org_id"),  # for the (message, org) foreign key of citations
        CheckConstraint("role IN ('user', 'assistant')", name="role"),
        CheckConstraint("status IN ('streaming', 'done', 'failed')", name="status"),
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
        ForeignKeyConstraint(
            ["conversation_id", "org_id"],
            ["conversations.id", "conversations.org_id"],
            ondelete="CASCADE",
        ),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column()
    role: Mapped[str] = mapped_column(String(16))
    # The message as the client renders it (the UI message parts: text, tool calls, data).
    parts: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, server_default="[]")
    status: Mapped[str] = mapped_column(String(16), default="done", server_default="done")
    confidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    abstained: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("snapshots.id", ondelete="SET NULL"), index=True
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column()  # its agent run (4.8)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class MessageCitation(IdMixin, OrgScopedMixin, Base):
    __tablename__ = "citations"
    __table_args__ = (
        UniqueConstraint("message_id", "idx"),
        Index("ix_citations_repo_id", "repo_id"),  # deleting a repository cascades here
        ForeignKeyConstraint(
            ["message_id", "org_id"], ["messages.id", "messages.org_id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["repo_id", "org_id"], ["repos.id", "repos.org_id"], ondelete="CASCADE"
        ),
        CheckConstraint("start_line >= 1 AND end_line >= start_line", name="lines"),
    )

    message_id: Mapped[uuid.UUID] = mapped_column()
    idx: Mapped[int] = mapped_column(Integer)  # its number in the answer, from 1
    repo_id: Mapped[uuid.UUID] = mapped_column()
    sha: Mapped[str] = mapped_column(String(40))
    path: Mapped[str] = mapped_column(Text)
    start_line: Mapped[int] = mapped_column(Integer)
    end_line: Mapped[int] = mapped_column(Integer)
    symbol_key: Mapped[str | None] = mapped_column(Text)
    provenance: Mapped[str] = mapped_column(String(32))  # what found it: evidence, a tool
    verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")


class AnswerCacheEntry(IdMixin, OrgScopedMixin, Base):
    """An answer kept for the same question again (4.9): at the same commit and mode, asked in
    the same words (or, with embeddings, nearly), it is served again after its cited files are
    checked unchanged. Gone with the answer."""

    __tablename__ = "answer_cache"
    __table_args__ = (
        Index("ix_answer_cache_lookup", "snapshot_id", "mode", "question_key"),
        Index("ix_answer_cache_repo_id", "repo_id"),
        ForeignKeyConstraint(
            ["message_id", "org_id"], ["messages.id", "messages.org_id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["repo_id", "org_id"], ["repos.id", "repos.org_id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["snapshot_id", "org_id"], ["snapshots.id", "snapshots.org_id"], ondelete="CASCADE"
        ),
    )

    repo_id: Mapped[uuid.UUID] = mapped_column()
    snapshot_id: Mapped[uuid.UUID] = mapped_column()
    mode: Mapped[str] = mapped_column(String(16))
    question_key: Mapped[str] = mapped_column(String(64))  # sha256 of the question, normalized
    question: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(HalfVector(EMBEDDING_DIMENSIONS))
    message_id: Mapped[uuid.UUID] = mapped_column(index=True)
    # The files its citations are in, by content hash, when it was kept.
    file_hashes: Mapped[dict[str, str]] = mapped_column(JSONB, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
