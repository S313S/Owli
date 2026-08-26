"""用户动作请求卡片的封闭契约与 SSE payload。"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from enum import Enum
from typing import Any


class CardType(str, Enum):
    HISTORY_REUSE = "HISTORY_REUSE"
    LOGIN_REPAIR = "LOGIN_REPAIR"
    AUTHORIZE = "AUTHORIZE"
    ENGINE_SWITCH_CONFIRM = "ENGINE_SWITCH_CONFIRM"
    EXTRA_QUOTA_CONFIRM = "EXTRA_QUOTA_CONFIRM"
    QUESTION = "QUESTION"
    ARTIFACT_OPEN = "ARTIFACT_OPEN"
    INTERVENE = "INTERVENE"


class CardActionType(str, Enum):
    OPEN_URL = "OPEN_URL"
    OPEN_FILE = "OPEN_FILE"
    TEXT_INPUT = "TEXT_INPUT"
    CHOICE_2 = "CHOICE_2"


class CardBlocking(str, Enum):
    NONE = "none"
    AGENT = "agent"
    GOAL = "goal"
    RESEARCH = "research"


class CardStatus(str, Enum):
    PENDING = "pending"
    ANSWERED = "answered"
    EXPIRED_DEFAULTED = "expired_defaulted"
    CANCELLED = "cancelled"


# 同时提供按字段名直觉可发现的别名，枚举成员仍只有契约规定的闭集。
ActionType = CardActionType
Blocking = CardBlocking
BlockingType = CardBlocking
Status = CardStatus


@dataclass
class Card:
    card_id: str
    card_type: CardType
    research_id: str
    goal_id: str | None
    agent_id: str | None
    title: str
    body: str
    target: dict[str, Any]
    actions: list[CardActionType | dict[str, Any]]
    blocking: CardBlocking
    deadline: str | None
    status: CardStatus
    result: dict[str, Any] | None
    created_at: str
    resolved_at: str | None

    def __post_init__(self) -> None:
        self.card_type = CardType(self.card_type)
        normalized_actions: list[dict[str, Any]] = []
        for action in self.actions:
            if isinstance(action, dict):
                normalized = copy.deepcopy(action)
                normalized["type"] = CardActionType(normalized.get("type")).value
            else:
                normalized = {"type": CardActionType(action).value}
            normalized_actions.append(normalized)
        self.actions = normalized_actions
        self.blocking = CardBlocking(self.blocking)
        self.status = CardStatus(self.status)
        if not self.actions:
            raise ValueError("卡片 actions 至少需要 1 个动作")

    def to_dict(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "card_type": self.card_type.value,
            "research_id": self.research_id,
            "goal_id": self.goal_id,
            "agent_id": self.agent_id,
            "title": self.title,
            "body": self.body,
            "target": copy.deepcopy(self.target),
            "actions": copy.deepcopy(self.actions),
            "blocking": self.blocking.value,
            "deadline": self.deadline,
            "status": self.status.value,
            "result": copy.deepcopy(self.result),
            "created_at": self.created_at,
            "resolved_at": self.resolved_at,
        }

    def to_event(self) -> dict[str, Any]:
        """交给 ResearchEventBuffer.publish，沿既有 SSE 形状发送。"""
        return {"type": "card_update", "data": {"card": self.to_dict()}}
