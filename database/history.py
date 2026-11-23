"""Conversation persistence for trusted callers, without prompts or model calls."""

import json
from datetime import datetime
from functools import wraps
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .models import Conversation, Message, Organization
from tools.contracts import ToolError, ToolErrorCode as E, ToolResult


class FunctionCall(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1)
    arguments: str


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1, max_length=255)
    type: Literal["function"] = "function"
    function: FunctionCall


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    role: Literal["user", "assistant", "tool"]
    content: str | list[dict[str, Any]] | dict[str, Any] | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = Field(default=None, min_length=1, max_length=255)

    @model_validator(mode="after")
    def valid_envelope(self):
        if self.tool_calls is not None and (not self.tool_calls or self.role != "assistant" or len({c.id for c in self.tool_calls}) != len(self.tool_calls)):
            raise ValueError("Only assistants may issue distinct tool calls")
        if (self.role == "tool") != (self.tool_call_id is not None):
            raise ValueError("Tool results require a tool_call_id; other roles cannot have it")
        if self.content is None and not self.tool_calls:
            raise ValueError("Content or assistant tool calls are required")
    
        json.dumps(self.model_dump(mode="json"), allow_nan=False)

        return self

    def to_api(self):
        """Return an API message; application JSON content becomes unprivileged text."""
    
        data = self.model_dump(mode="json", exclude_none=True)
        if isinstance(self.content, dict):
            data["content"] = json.dumps(self.content, ensure_ascii=False)
    
        if self.content is None:
            data["content"] = None

        return data


class SavedMessage(ChatMessage):
    id: str
    sequence: int
    created_at: datetime

    def to_api(self):
        return ChatMessage(**{k: getattr(self, k) for k in ChatMessage.model_fields}).to_api()


class ConversationInfo(BaseModel):
    id: str
    organization_id: str
    user_id: str
    created_at: datetime


class RecentMessages(BaseModel):
    messages: list[SavedMessage]
    pending_tool_calls: list[ToolCall]
    incomplete_group: list[SavedMessage] = Field(default_factory=list)
    omitted_partial_group: bool


class _HistoryError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


def _guard(function):
    @wraps(function)
    def call(session, organization_id, user_id, *args, **kwargs):
        try:
            if any(not isinstance(v, str) or not v.strip() or len(v) > limit
                   for v, limit in ((organization_id, 100), (user_id, 255))):
                raise ValueError("Trusted organization and user identifiers are required")
            if not isinstance(session, Session):
                raise ValueError("A SQLAlchemy Session is required")
            if session.new or session.dirty or session.deleted:
                raise ValueError("Flush pending changes before accessing history")
            with session.begin_nested():
                data = function(session, organization_id, user_id, *args, **kwargs)
                session.flush()
                return ToolResult[type(data)](ok=True, data=data)

        except _HistoryError as exc:
            code, message = exc.code, exc.message
        except (ValueError, TypeError, ValidationError):
            code, message = E.INVALID_ARGUMENTS, "Invalid history arguments or message sequence"
        except SQLAlchemyError:
            code, message = E.DATABASE_ERROR, "Unable to persist or read conversation history"
    
        return ToolResult(ok=False, error=ToolError(code=code, message=message, operation_outcome="not_started"))

    return call


def _conversation(session, org, user, identity, lock=False):
    stmt = select(Conversation).where(Conversation.organization_id == org,
        Conversation.user_id == user, Conversation.id == identity).execution_options(populate_existing=True)
    row = session.scalar(stmt.with_for_update() if lock else stmt)
    if row is None:
        raise _HistoryError(E.NOT_FOUND, "Conversation not found for this user and organization")

    return row


def _timestamp(now):
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("An explicit aware timestamp is required")


def _saved(row):
    return SavedMessage(**{k: getattr(row, k) for k in SavedMessage.model_fields})


def _tail(session, org, conv):
    last = session.scalar(select(Message).where(Message.organization_id == org,
        Message.conversation_id == conv).order_by(Message.sequence.desc()).limit(1))
    if last is None:
        return [], []

    group = list(session.scalars(select(Message).where(Message.organization_id == org,
        Message.conversation_id == conv, Message.sequence >= last.group_start).order_by(Message.sequence)))

    calls = [ToolCall.model_validate(c) for c in (group[0].tool_calls or [])]
    resolved = {m.tool_call_id for m in group if m.role == "tool"}

    return group, [c for c in calls if c.id not in resolved]


@_guard
def create_conversation(session, organization_id, user_id, *, now: datetime) -> ToolResult[ConversationInfo]:
    _timestamp(now)

    if session.get(Organization, organization_id) is None:
        raise _HistoryError(E.NOT_FOUND, "Organization not found")

    row = Conversation(organization_id=organization_id, id=str(uuid4()), user_id=user_id, created_at=now)
    session.add(row)

    return ConversationInfo(id=row.id, organization_id=organization_id, user_id=user_id, created_at=now)


@_guard
def get_conversation(session, organization_id, user_id, conversation_id) -> ToolResult[ConversationInfo]:
    row = _conversation(session, organization_id, user_id, conversation_id)
    return ConversationInfo(**{k: getattr(row, k) for k in ConversationInfo.model_fields})


@_guard
def add_message(session, organization_id, user_id, conversation_id, *, message: ChatMessage,
                now: datetime, message_id: str | None = None) -> ToolResult[SavedMessage]:
    _timestamp(now)
    if not isinstance(message, ChatMessage):
        raise ValueError("A validated ChatMessage is required")

    message = ChatMessage.model_validate(message.model_dump())
    conv = _conversation(session, organization_id, user_id, conversation_id, lock=True)
    identity = message_id or str(uuid4())
    if not isinstance(identity, str) or not identity.strip() or len(identity) > 100:
        raise ValueError("Invalid message identity")

    existing = session.get(Message, (organization_id, identity))
    if existing:
        if existing.conversation_id != conversation_id or _saved(existing).to_api() != message.to_api():
            raise _HistoryError(E.DUPLICATE_OPERATION, "Message identifier already used")
        return _saved(existing)

    group, pending = _tail(session, organization_id, conversation_id)
    if now < (group[-1].created_at if group else conv.created_at):
        raise ValueError("Message time cannot go backwards")

    if pending:
        if message.role != "tool" or message.tool_call_id not in {c.id for c in pending}:
            raise ValueError("Resolve outstanding tool calls before continuing the dialog")
    elif message.role == "tool":
        raise ValueError("Tool result has no outstanding call")

    conv.last_sequence += 1
    row = Message(organization_id=organization_id, id=identity, conversation_id=conversation_id,
        sequence=conv.last_sequence, group_start=group[0].group_start if pending else conv.last_sequence,
        created_at=now, **message.model_dump(mode="json"))

    session.add(row)

    return _saved(row)


@_guard
def get_recent_messages(session, organization_id, user_id, conversation_id) -> ToolResult[RecentMessages]:
    _conversation(session, organization_id, user_id, conversation_id, lock=True)

    rows = list(reversed(list(session.scalars(select(Message).where(Message.organization_id == organization_id,
        Message.conversation_id == conversation_id).order_by(Message.sequence.desc()).limit(20)))))

    group, pending = _tail(session, organization_id, conversation_id)
    selected = [m for m in rows if m.group_start >= rows[0].sequence
                and (not pending or m.sequence < group[0].sequence)] if rows else []

    return RecentMessages(messages=[_saved(m) for m in selected], pending_tool_calls=pending,
                          incomplete_group=[_saved(m) for m in group] if pending else [],
                          omitted_partial_group=len(selected) != len(rows))
