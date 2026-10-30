"""Every ORM model, imported here so Alembic autogenerate sees the full schema."""

from __future__ import annotations

from sensei_core.db.base import Base
from sensei_core.models.agent_runs import AgentRun, AgentStep
from sensei_core.models.audit import ActorType, AuditEvent, UsageEvent, UsageKind
from sensei_core.models.chunks import CodeChunk
from sensei_core.models.conversations import (
    AnswerCacheEntry,
    Conversation,
    ConversationMessage,
    MessageCitation,
)
from sensei_core.models.embeddings import EmbeddingCache
from sensei_core.models.evals import EvalRun
from sensei_core.models.files import RepoFile
from sensei_core.models.github import GitHubInstallation
from sensei_core.models.glossary import GlossaryTerm
from sensei_core.models.graph import (
    GRAPH_KINDS,
    SYMBOL_KINDS,
    CodeEdge,
    EdgeType,
    NodeKind,
    Provenance,
)
from sensei_core.models.indexing import (
    ACTIVE_JOB_STATUSES,
    IndexJob,
    JobKind,
    JobStatus,
    Snapshot,
    SnapshotStatus,
    Stage,
)
from sensei_core.models.jobs import JobFailure
from sensei_core.models.llm import LLMBatch, LLMBatchStatus
from sensei_core.models.org import Invitation, Membership, Org, Role
from sensei_core.models.repo import Repo, RepoSource, RepoStatus
from sensei_core.models.symbols import CodeNode
from sensei_core.models.user import OAuthToken, User, UserSession
from sensei_core.models.webhook import DeliveryStatus, WebhookDelivery

__all__ = [
    "ACTIVE_JOB_STATUSES",
    "GRAPH_KINDS",
    "SYMBOL_KINDS",
    "ActorType",
    "AgentRun",
    "AgentStep",
    "AnswerCacheEntry",
    "AuditEvent",
    "Base",
    "CodeChunk",
    "CodeEdge",
    "CodeNode",
    "Conversation",
    "ConversationMessage",
    "DeliveryStatus",
    "EdgeType",
    "EmbeddingCache",
    "EvalRun",
    "GitHubInstallation",
    "GlossaryTerm",
    "IndexJob",
    "Invitation",
    "JobFailure",
    "JobKind",
    "JobStatus",
    "LLMBatch",
    "LLMBatchStatus",
    "Membership",
    "MessageCitation",
    "NodeKind",
    "OAuthToken",
    "Org",
    "Provenance",
    "Repo",
    "RepoFile",
    "RepoSource",
    "RepoStatus",
    "Role",
    "Snapshot",
    "SnapshotStatus",
    "Stage",
    "UsageEvent",
    "UsageKind",
    "User",
    "UserSession",
    "WebhookDelivery",
]
