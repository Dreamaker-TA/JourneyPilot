"""LangChain 消息构建工具。"""

from __future__ import annotations

from typing import Optional

from langchain_core.messages import AIMessage, HumanMessage


def build_messages(conversation_history: Optional[list], user_message: str) -> list:
    """将对话历史和用户消息转换为 LangChain 消息列表。"""
    messages = []
    if conversation_history:
        for msg in conversation_history:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "user":
                messages.append(HumanMessage(content=content))
            elif role == "assistant":
                messages.append(AIMessage(content=content))

    messages.append(HumanMessage(content=user_message))
    return messages
