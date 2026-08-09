"""
统一 Embedding 层。

provider 三选一：
1. "qwen3"  — 本地 Qwen3-Embedding-0.6B ONNX 推理（默认；首次启动从 HF 下载 ~1.2GB）
2. "openai" — OpenAI 兼容 Embedding API（需 api_key + base_url）
3. "hash"   — 内置确定性哈希向量（零依赖，仅供 RAG 链路跑通）

只有显式 provider="hash" 才使用 HashEmbedder；真实 provider 初始化失败必须显式报错。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from ..config import get_settings

logger = logging.getLogger(__name__)


@runtime_checkable
class BaseEmbedder(Protocol):
    """本模块使用的 Embedding 协议类型（duck typing）。"""

    async def embed(self, text: str) -> List[float]: ...

    async def embed_batch(self, texts: List[str]) -> List[List[float]]: ...

    @property
    def dimensions(self) -> int: ...

try:
    from openai import AsyncOpenAI
except Exception:  # pragma: no cover - import fallback
    AsyncOpenAI = None  # type: ignore[assignment]


def _normalize_vector(values: List[float]) -> List[float]:
    norm = math.sqrt(sum(v * v for v in values))
    if norm <= 0:
        return values
    return [v / norm for v in values]


def _tokenize(text: str) -> List[str]:
    if not text:
        return []
    tokens = re.findall(r"[\w\u4e00-\u9fff]+", text.lower())
    if tokens:
        return tokens
    return [text.lower()]


class HashEmbedder(BaseEmbedder):
    """内置确定性哈希嵌入。"""

    def __init__(self, dimensions: int) -> None:
        self._dimensions = max(32, int(dimensions))

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def _vectorize(self, text: str) -> List[float]:
        vector = [0.0] * self._dimensions
        tokens = _tokenize(text)
        if not tokens:
            return vector

        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            for offset in range(0, min(len(digest), 24), 4):
                bucket = int.from_bytes(digest[offset : offset + 2], "big") % self._dimensions
                sign = 1.0 if digest[offset + 2] % 2 == 0 else -1.0
                weight = 1.0 + (digest[offset + 3] / 255.0)
                vector[bucket] += sign * weight
        return _normalize_vector(vector)

    async def embed(self, text: str) -> List[float]:
        return self._vectorize(text)

    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        return [self._vectorize(text) for text in texts]


class OpenAICompatibleEmbedder(BaseEmbedder):
    """OpenAI 兼容 Embedding 包装。"""

    def __init__(self, *, api_key: str, base_url: str, model_name: str, dimensions: int) -> None:
        if AsyncOpenAI is None:
            raise RuntimeError("openai 包不可用，无法创建真实 Embedding 客户端")

        self._dimensions = int(dimensions)
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self._model_name = model_name

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, text: str) -> List[float]:
        kwargs: Dict[str, Any] = {"model": self._model_name, "input": text}
        if self._dimensions:
            kwargs["dimensions"] = self._dimensions
        response = await self._client.embeddings.create(**kwargs)
        return self._validate_vector(list(response.data[0].embedding))

    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        kwargs: Dict[str, Any] = {"model": self._model_name, "input": texts}
        if self._dimensions:
            kwargs["dimensions"] = self._dimensions
        response = await self._client.embeddings.create(**kwargs)
        return [self._validate_vector(list(item.embedding)) for item in response.data]

    def _validate_vector(self, vector: List[float]) -> List[float]:
        if self._dimensions and len(vector) != self._dimensions:
            raise RuntimeError(
                f"Embedding 维度不匹配：配置为 {self._dimensions}，实际返回 {len(vector)}；"
                "请调整 embedding.dimensions 或更换支持该维度的 embedding 模型"
            )
        return vector


class Qwen3OnnxEmbedder(BaseEmbedder):
    """Qwen3-Embedding-0.6B 本地 ONNX 推理器。

    权重首次运行通过 huggingface_hub 下载到 ~/.cache/huggingface/（约 1.2 GB），
    推理走 onnxruntime CPU；分词走 tokenizers（Rust bindings），不引入 transformers/torch。
    输出经 mean-pooling + L2 归一化，生成 1024 维向量。
    """

    _MAX_SEQ_LEN = 512

    def __init__(self, model_repo: str) -> None:
        from huggingface_hub import snapshot_download
        import onnxruntime as ort
        from tokenizers import Tokenizer

        model_dir = Path(
            snapshot_download(
                repo_id=model_repo,
                allow_patterns=[
                    "*.onnx", "*.onnx_data",
                    "tokenizer.json", "tokenizer_config.json",
                    "special_tokens_map.json", "config.json",
                ],
            )
        )
        onnx_path = self._find_onnx_model(model_dir)
        if onnx_path is None:
            raise FileNotFoundError(f"未在 {model_dir} 下找到 *.onnx 权重")

        tokenizer_path = model_dir / "tokenizer.json"
        if not tokenizer_path.exists():
            raise FileNotFoundError(f"未找到 tokenizer.json（expected at {tokenizer_path}）")

        session_options = ort.SessionOptions()
        session_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        # 关闭内存复用，避免动态 batch_size 切换时复用旧 shape buffer 引发的 RUNTIME_EXCEPTION
        session_options.enable_mem_pattern = False
        session_options.enable_mem_reuse = False
        self._session = ort.InferenceSession(
            str(onnx_path),
            sess_options=session_options,
            providers=["CPUExecutionProvider"],
        )
        self._tokenizer = Tokenizer.from_file(str(tokenizer_path))
        self._input_names = {inp.name for inp in self._session.get_inputs()}
        self._dimensions = 1024  # Qwen3-Embedding-0.6B 固定输出维度

    @staticmethod
    def _find_onnx_model(model_dir: Path) -> Optional[Path]:
        candidates = list(model_dir.rglob("*.onnx"))
        if not candidates:
            return None
        # ONNX 仓库可能包含多个 .onnx（原图 + 优化图），选最大的主权重
        return max(candidates, key=lambda p: p.stat().st_size)

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, text: str) -> List[float]:
        vectors = await self.embed_batch([text])
        return vectors[0]

    # 单批 ONNX 推理上限：超过此值拆分调用，避免 Expand / 内存复用引发的 RUNTIME_EXCEPTION
    # batch=1 因为该 Qwen3 ONNX 图存在 Expand 节点对动态 batch 维敏感
    _EMBED_MICRO_BATCH = 1

    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        if len(texts) <= self._EMBED_MICRO_BATCH:
            return await asyncio.to_thread(self._embed_sync, texts)
        # 大 batch 拆分为 micro-batch 串行调用
        results: List[List[float]] = []
        for i in range(0, len(texts), self._EMBED_MICRO_BATCH):
            chunk = texts[i : i + self._EMBED_MICRO_BATCH]
            partial = await asyncio.to_thread(self._embed_sync, chunk)
            results.extend(partial)
        return results

    def _embed_sync(self, texts: List[str]) -> List[List[float]]:
        import numpy as np

        encoded = self._tokenizer.encode_batch(texts)
        max_len = max((len(e.ids) for e in encoded), default=1)
        max_len = min(max_len, self._MAX_SEQ_LEN)

        batch_ids: List[List[int]] = []
        batch_mask: List[List[int]] = []
        for enc in encoded:
            ids = list(enc.ids[:max_len])
            mask = list(enc.attention_mask[:max_len])
            pad = max_len - len(ids)
            if pad > 0:
                ids.extend([0] * pad)
                mask.extend([0] * pad)
            batch_ids.append(ids)
            batch_mask.append(mask)

        input_ids = np.asarray(batch_ids, dtype=np.int64)
        attention_mask = np.asarray(batch_mask, dtype=np.int64)

        feed: Dict[str, Any] = {}
        if "input_ids" in self._input_names:
            feed["input_ids"] = input_ids
        if "attention_mask" in self._input_names:
            feed["attention_mask"] = attention_mask
        if "token_type_ids" in self._input_names:
            feed["token_type_ids"] = np.zeros_like(input_ids)

        outputs = self._session.run(None, feed)
        raw = outputs[0]

        if raw.ndim == 3:
            # (batch, seq, hidden) → mean pooling with attention mask
            mask_f = attention_mask.astype(np.float32)[..., None]
            pooled = (raw * mask_f).sum(axis=1) / np.clip(mask_f.sum(axis=1), 1e-6, None)
        elif raw.ndim == 2:
            pooled = raw
        else:
            raise RuntimeError(f"Qwen3 ONNX 输出形状异常：{raw.shape}")

        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        norms = np.where(norms < 1e-12, 1.0, norms)
        normed = (pooled / norms).astype(np.float32)

        if normed.shape[1] != self._dimensions:
            self._dimensions = int(normed.shape[1])

        return normed.tolist()


_embedder: Optional[BaseEmbedder] = None


def _build_embedder() -> BaseEmbedder:
    cfg = get_settings().embedding

    if cfg.provider == "qwen3":
        embedder = Qwen3OnnxEmbedder(cfg.model_name or "n24q02m/Qwen3-Embedding-0.6B-ONNX")
        logger.info("Embedding 使用 Qwen3 ONNX (model=%s, dimensions=%d)", cfg.model_name, embedder.dimensions)
        if cfg.dimensions and embedder.dimensions != int(cfg.dimensions):
            raise RuntimeError(
                f"Qwen3 Embedding 维度不匹配：配置为 {cfg.dimensions}，模型输出 {embedder.dimensions}"
            )
        return embedder

    if cfg.provider == "openai" and cfg.api_key and cfg.base_url and cfg.model_name:
        embedder = OpenAICompatibleEmbedder(
            api_key=cfg.api_key,
            base_url=cfg.base_url,
            model_name=cfg.model_name,
            dimensions=cfg.dimensions,
        )
        logger.info("Embedding 使用 OpenAI 兼容 API (model=%s, dimensions=%d)", cfg.model_name, cfg.dimensions)
        return embedder

    if cfg.provider == "hash":
        logger.warning("Embedding 使用 HashEmbedder (dimensions=%d)，仅适用于本地 smoke test", cfg.dimensions)
        return HashEmbedder(cfg.dimensions)

    raise RuntimeError(
        f"Embedding provider 配置不可用: provider={cfg.provider!r}；"
        "请配置 qwen3/openai 的真实参数，或显式设置 provider='hash' 仅用于 smoke test"
    )


def get_embedder() -> BaseEmbedder:
    global _embedder
    if _embedder is None:
        _embedder = _build_embedder()
    return _embedder
