"""
快速模式工作流 (Application Layer)

简化版：
  START → fast_answer_agent → END

fast_answer_agent 纯知识问答，不调用工具。
遇到超出能力边界的请求时，在回答中用任务语言引导到相应旅行流程。
"""

from __future__ import annotations

import logging
from typing import Any, AsyncGenerator, Dict, List, Optional

from langgraph.graph import END, START, StateGraph

from ..entities.trip_run import generate_trip_run_id
from ..agents.fast_answer.node import fast_answer_node
from ..entities.state import TravelAgentState
from ..local_profile import LOCAL_USER_ID
from ..utils.message_helpers import build_messages
from .run_control import run_attribution, with_run_control

logger = logging.getLogger(__name__)

NODE_FAST_ANSWER = "fast_answer_agent"


def build_fast_workflow() -> StateGraph:
    """构建快速模式工作流图（fast_answer_agent → END）。"""
    graph = StateGraph(TravelAgentState)
    graph.add_node(NODE_FAST_ANSWER, with_run_control(NODE_FAST_ANSWER, fast_answer_node))
    graph.add_edge(START, NODE_FAST_ANSWER)
    graph.add_edge(NODE_FAST_ANSWER, END)
    return graph


_compiled_fast_graph = None


def get_fast_graph():
    """获取编译后的快速模式工作流图（单例）"""
    global _compiled_fast_graph
    if _compiled_fast_graph is None:
        workflow = build_fast_workflow()
        _compiled_fast_graph = workflow.compile()
        logger.info("LangGraph 快速模式工作流编译完成（简化版）")
    return _compiled_fast_graph


class FastAnswerWorkflow:
    """快速模式工作流封装类。"""

    def __init__(self) -> None:
        self._graph = get_fast_graph()

    async def run(
        self,
        user_message: str,
        session_id: str,
        user_id: str = LOCAL_USER_ID,
        selected_mcp_servers: Optional[List[str]] = None,
        current_time: str = "",
        conversation_history: Optional[list] = None,
        session_anchor: Optional[Dict[str, Any]] = None,
        session_compressed: bool = False,
        preset_context: str = "",
        preset_pack_constraints: Optional[Dict[str, str]] = None,
        route_decision: Optional[Dict[str, Any]] = None,
        run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """执行一轮快速问答。"""
        import datetime

        messages = build_messages(conversation_history, user_message)

        initial_state = TravelAgentState(
            messages=messages,
            session_id=session_id,
            user_id=user_id,
            selected_mcp_servers=selected_mcp_servers or [],
            user_query=user_message,
            run_id=run_id or generate_trip_run_id(),
            current_time=current_time or datetime.datetime.now().strftime("%Y-%m-%d %A %H:%M:%S"),
            session_anchor=session_anchor,
            session_compressed=session_compressed,
            preset_context=preset_context,
            preset_pack_constraints=dict(preset_pack_constraints or {}),
            route_decision=route_decision or {},
        )

        config = {"configurable": {"thread_id": initial_state.run_id}}
        with run_attribution(initial_state.run_id):
            final_state = await self._graph.ainvoke(initial_state, config=config)
        return final_state

    async def astream(
        self,
        user_message: str,
        session_id: str,
        user_id: str = LOCAL_USER_ID,
        selected_mcp_servers: Optional[List[str]] = None,
        current_time: str = "",
        conversation_history: Optional[list] = None,
        stream_queue=None,
        session_anchor: Optional[Dict[str, Any]] = None,
        session_compressed: bool = False,
        preset_context: str = "",
        preset_pack_constraints: Optional[Dict[str, str]] = None,
        route_decision: Optional[Dict[str, Any]] = None,
        run_id: Optional[str] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """流式执行快速模式工作流。"""
        import datetime

        messages = build_messages(conversation_history, user_message)

        initial_state = TravelAgentState(
            messages=messages,
            session_id=session_id,
            user_id=user_id,
            selected_mcp_servers=selected_mcp_servers or [],
            user_query=user_message,
            run_id=run_id or generate_trip_run_id(),
            current_time=current_time or datetime.datetime.now().strftime("%Y-%m-%d %A %H:%M:%S"),
            session_anchor=session_anchor,
            session_compressed=session_compressed,
            preset_context=preset_context,
            preset_pack_constraints=dict(preset_pack_constraints or {}),
            route_decision=route_decision or {},
        )

        config = {"configurable": {"thread_id": initial_state.run_id}}
        if stream_queue is not None:
            config["configurable"]["stream_queue"] = stream_queue
        # 双 stream_mode（与深度工作流同构，JP-02-04 §A.2）：updates 帧逐节点透传；
        # values 帧通过内部 queue 持久化当前全量 state，并留末帧供 __final_state__ 使用。
        # fast 全量 state 无 verifier 产物 → evidence / risk builder 返 None（§C.1），constraint 仍可出。
        last_values = None
        async def lifecycle_sink(payload: Dict[str, Any]) -> None:
            if stream_queue is not None:
                await stream_queue.put(("node_lifecycle", payload))

        with run_attribution(
            initial_state.run_id,
            lifecycle_sink=lifecycle_sink if stream_queue is not None else None,
        ):
            async for mode, chunk in self._graph.astream(
                initial_state, stream_mode=["updates", "values"], config=config
            ):
                if mode == "updates":
                    yield chunk
                else:  # "values"
                    last_values = chunk
                    if stream_queue is not None and isinstance(last_values, dict):
                        await stream_queue.put(("state_snapshot", last_values))
        if last_values is not None:
            yield {"__final_state__": last_values}
