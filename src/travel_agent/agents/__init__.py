"""
JourneyPilot 多智能体系统 — Agent 节点层

本项目包含三种 Agent 模式：

1. **Streaming ReAct Agent** — 工具驱动的循环推理
   - 节点：destination_researcher, transport_researcher, accommodation_researcher
   - 特点：调用 MCP 工具 → 观察结果 → 决策下一步 → 循环直到满足条件
   - 驱动函数：agents/utils.py 的 streaming_react_loop()

2. **Pure LLM Agent** — 直接 LLM 推理
   - 节点：scope/clarifier, scope/brief_generator, itinerary_planner, synthesizer, fast_answer
   - 特点：无工具调用，依赖上下文和上游 Agent 输出，单次 LLM 调用完成任务

3. **Orchestration Node** — 编排与决策
   - LLM 规划：planner
   - 纯状态机：dispatcher（无 LLM，基于状态路由）
   - 确定性门：candidate_gate, artifact_gate, delivery_quality_gate
"""
