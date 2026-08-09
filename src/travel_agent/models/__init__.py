"""模型层统一入口（LLM 路由 + Embedding 实现收敛到此目录）。"""

from .embedder import get_embedder
from .router import ModelRouter, ModelTier, get_model_router

__all__ = [
    "ModelRouter",
    "ModelTier",
    "get_model_router",
    "get_embedder",
]
