# JourneyPilot Trip Workspace

JourneyPilot Trip Workspace 的前端——React 18 + TypeScript + Vite + Tailwind CSS + Leaflet 构建的 TripOps 工作台。前端走单一 live 路径，直接对接 FastAPI + LangGraph 多 Agent 后端。

## 架构

- 聊天流：`src/lib/sse.ts` 消费 `POST /api/chat-stream` 的 SSE 响应，逐帧解析并驱动 UI。
- REST：`src/lib/api.ts` 覆盖 current/immutable Delivery Bundle、Workspace mutation、undo/restore、天气刷新、服务端 PDF、TripRun 事件、会话、记忆、知识库和预设等端点，经 `VITE_API_BASE` 指向后端。
- 主路径 contract：Trip Workspace 只确认完整 `DeliveryBundle`。报告、行程、地图、来源、天气和历史共享同一 manifest；聊天正文和乐观预览都不是正式事实源。
- 正式 Deep SSE：过程事件可以流式，但交付只接受一次 `delivery_ready` 和一次匹配的 `run_terminal`；断线恢复按 durable event cursor 读取不可变 Bundle。
- 完整系统基于 FastAPI + LangGraph 多 Agent + MCP 工具生态 + pgvector + PostgreSQL 全文检索 + RRF、五层记忆系统构建。

## 本地运行

```bash
npm install
npm run dev   # http://localhost:8080
```

`vite.config.ts` 已把 `/api` 代理到 `http://127.0.0.1:8001`。如果后端在其它地址，用 `VITE_API_BASE` 覆盖：

```bash
VITE_API_BASE=/api npm run dev
```

LLM provider key 只应配置在后端环境中，不应写入前端 `.env`。

## 构建与类型检查

```bash
npm run build        # 产物写入 ../static（VITE_OUT_DIR 可覆盖）
npm run type-check   # tsc --noEmit
```

子路径部署时用 `VITE_BASE` 覆盖 base：

```bash
VITE_BASE=/journeypilot/ VITE_OUT_DIR=dist npm run build
```
