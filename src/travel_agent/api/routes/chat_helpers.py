"""
聊天路由共享辅助函数。

从 chat.py 提取的纯函数 / IO 辅助，供 chat_stream 和 chat 两个端点共用，
消除重复逻辑的同时降低 chat.py 文件体积。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Dict, Optional

from fastapi import HTTPException

from ...guardrails.input_guard import InputGuard
from ...memory.context_builder import ContextBudget

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 会话 & 数据加载
# ---------------------------------------------------------------------------

async def load_user_profile(user_profile_memory, user_id: str):
    """加载用户画像对象；取不到就是 ``None``。

    **这里不再拼任何「画像摘要」字符串。** 画像抵达模型的通道只有一条：
    Constraint Pack 的统一装配入口自己读取 ``UserProfileMemory``
    （``panels/constraint.py::_map_manual_profile`` 映六组偏好、``_auto_portrait_block``
    映系统画像）。此前这里还会把偏好与画像拼成一段散文塞进 state，只有快路径读它 ——
    于是同一份节奏 / 预算在快路径上是一句不参与仲裁的散文、在深度路径上是 pack 里可被
    preset 压过的一条 item，两条路各有各的赢家。路由这一侧现在只把对象交出去，
    唯一的读者是记忆抽取（它要 ``auto_portrait`` 当增量基线）。
    """
    try:
        return await user_profile_memory.get_user_profile(user_id)
    except Exception as e:
        logger.warning(f"加载用户画像失败: {e}")
        return None


async def load_preset_context(
    preset_id: Optional[str], user_id: str
) -> tuple[str, Dict[str, str]]:
    """加载预设，返回 (进 prompt 的那段文本, 交给 Constraint Pack 的那几项)。

    交两份出去而不是一份，是因为一个 preset 里有两类东西，归两层负责：指令与重点关注
    是给模型看的散文，节奏与预算档位是可执行约束、要进 pack 参与仲裁（见
    ``PresetInjector.PRESET_PACK_CONSTRAINT_CATEGORIES``）。在一份字符串里同时表达
    这两件事，就是那三个「风格」互不相认的起点。

    - 未传 preset_id：返回空串与空字典（不注入风格）。
    - 显式 preset 不存在或无权访问：HTTP 404（资源语义，非 500）。
    - 存储层异常：HTTP 500，明确业务文案。
    """
    if not preset_id:
        return "", {}
    from fastapi import HTTPException

    from ...preset.injector import PresetInjector
    from ...preset.store import PresetStore

    try:
        preset_store = PresetStore()
        preset = await preset_store.get_preset(preset_id, user_id=user_id or None)
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"加载预设失败: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="加载旅行风格失败，请稍后重试",
        ) from e

    if not preset:
        raise HTTPException(
            status_code=404,
            detail="选择的旅行风格不存在或无权访问，请重新选择",
        )

    try:
        context = PresetInjector.build_context(preset)
        pack_constraints = PresetInjector.pack_constraints(preset)
        await preset_store.increment_usage(preset_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"预设上下文构建失败: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="加载旅行风格失败，请稍后重试",
        ) from e

    logger.info(f"预设已加载: {preset.name} (id={preset_id})")
    return context, pack_constraints


async def check_input_guard(user_message: str):
    """执行 InputGuard 检查，返回 guard result 对象。"""
    guard = InputGuard()
    return await guard.check(user_message)


async def load_session_history(
    chat_session_memory,
    user_id: str,
    session_id: str,
    mode: str,
    title_seed: str,
    controlled_trip_identity,
    *,
    load_anchor: bool = False,
) -> tuple:
    """
    确保会话存在并加载近期消息。
    返回 (history, anchor_data_or_None, is_compressed)。

    `controlled_trip_identity` 原样转给 `ensure_session`——会话标题的判据在那里，
    这一层只是它唯一的调用路径之一，不自己解释。
    """
    try:
        await chat_session_memory.ensure_session(
            session_id=session_id,
            user_id=user_id,
            mode=mode,
            title_seed=title_seed,
            controlled_trip_identity=controlled_trip_identity,
        )
        # 载入到压缩阈值为止，而不是写死条数。ContextBuilder 之后
        # 会把消息层裁到 messages_budget，所以这里多取的部分不会进 prompt——它
        # 的作用是让「组装后总估算 ≥ 阈值」这个判断**看得见足够的历史**。写死
        # 20 条时它永远只看得见 4,247 token，自动压缩因此结构性不可达。
        history = await chat_session_memory.get_recent_messages_within_token_budget(
            user_id=user_id,
            session_id=session_id,
            token_budget=ContextBudget().compaction_trigger_tokens,
        )
        anchor_data = None
        is_compressed = False
        if load_anchor:
            anchor_raw, compression_count = await chat_session_memory.get_anchor(
                user_id=user_id,
                session_id=session_id,
            )
            anchor_data = anchor_raw
            is_compressed = compression_count > 0
        return history, anchor_data, is_compressed
    except PermissionError:
        raise HTTPException(status_code=403, detail="无权访问该会话")
    except Exception as e:
        logger.error(f"会话历史加载失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="会话历史服务不可用")


async def enqueue_memory_extraction(
    *,
    user_id: str,
    session_id: str,
    user_message: str,
    user_message_id: str,
    assistant_message_id: str,
    profile_revision: int,
    portrait_baseline: str,
    background_job_worker=None,
) -> None:
    """把这一轮的记忆抽取排进 `background_jobs`。

    在聊天保存事务之后调用：任务只带引用，正文由 worker 从会话记录读回。画像基线随
    payload 固定，延迟几小时才执行的抽取不会改用另一版画像去判「这条已经知道了」。
    """
    if not user_message or not user_message_id:
        return
    from ...entities.background_job import (
        BackgroundJobType,
        memory_extraction_dedupe_key,
    )
    from ...infrastructure.background_job_store import get_background_job_store

    try:
        await get_background_job_store().enqueue(
            BackgroundJobType.MEMORY_EXTRACTION,
            memory_extraction_dedupe_key(session_id, assistant_message_id),
            {
                "user_id": user_id,
                "session_id": session_id,
                "user_message_id": user_message_id,
                "assistant_message_id": assistant_message_id,
                "profile_revision": profile_revision,
                "portrait_baseline": portrait_baseline,
            },
        )
    except Exception as e:
        logger.warning(f"记忆抽取入队失败: {e}")
        return
    if background_job_worker is not None:
        background_job_worker.notify()


# ---------------------------------------------------------------------------
# SSE & 文本处理
# ---------------------------------------------------------------------------

def sse_event(data: dict) -> str:
    """格式化为 SSE 事件字符串"""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


# 精确匹配：以推理关键词开头，到"连续空行+正文特征"处截止的推理块
# 正文特征复用 StreamingStripper._THINKING_END_RE 的同一判定逻辑
_THINKING_TEXT_PATTERN = re.compile(
    r'^(?:Thinking Process|thinking process|Let me analyze|Let me think|思考过程)'
    r'[\s\S]*?(?=\n\n(?=[^\x00-\x7F#\-\*]|\s*#{1,3}\s|\s*[-*]\s))',
    re.MULTILINE,
)
# 宽松兜底：整个响应若以推理关键词开头且无法识别正文边界，则整段视为推理
# 针对 Qwen3 等模型偶尔输出的纯英文 Chain-of-Thought 长段
_LEADING_ENGLISH_THINKING_PATTERN = re.compile(
    r'^(?:Thinking Process|thinking process|Let me analyze|Let me think)[\s\S]+',
)


def strip_thinking_text(content: str) -> str:
    """
    移除 LLM 输出中的纯文本推理过程（Qwen3 等模型可能输出 "Thinking Process:" 等英文推理块）。
    作为最终兜底，在内容写入前端和数据库之前调用。
    """
    if not content:
        return content

    # 优先尝试精确模式：从 Thinking 关键词到正文开始的分隔处
    cleaned = _THINKING_TEXT_PATTERN.sub("", content).lstrip()
    if cleaned and cleaned != content:
        return cleaned

    # 宽松兜底：若整个内容都是纯英文推理，返回原内容（避免误删）
    # 只有当内容中存在非 ASCII 字符（如中文）或 Markdown 标题时才尝试截断
    has_cjk = bool(re.search(r'[\u4e00-\u9fff]', content))
    has_markdown = bool(re.search(r'\n#{1,3}\s', content))
    if not has_cjk and not has_markdown:
        # 纯英文内容：可能本来就是正常英文回复，不过滤
        return content

    # 有中文/Markdown 正文：尝试从第一个 Markdown 标题或中文段落开始截取
    for marker in ("Thinking Process", "thinking process", "Let me analyze", "Let me think", "思考过程"):
        if content.lstrip().startswith(marker):
            # 找到第一个 "## " 或 "\n\n" 后接中文的位置
            m = re.search(r'\n\n(?=[^\x00-\x7F]|\s*#{1,3}\s|\s*[-*]\s)', content)
            if m:
                candidate = content[m.start():].lstrip()
                if candidate:
                    return candidate
            break

    return content

