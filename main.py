"""
JourneyPilot v2.0 - 新后端入口
运行方式：uv run python main.py 或 uvicorn main:app --reload
"""

import logging
import sys
from pathlib import Path

# 确保 src 目录在 Python 路径中
sys.path.insert(0, str(Path(__file__).parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

import uvicorn

from travel_agent.config import get_settings

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.logging.level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

from travel_agent.api.app import create_app

# 创建 FastAPI 应用
app = create_app()

if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("JourneyPilot v2.0 启动")
    logger.info(f"地址: http://{settings.server.host}:{settings.server.port}")
    logger.info(f"文档: http://{settings.server.host}:{settings.server.port}/docs")
    logger.info(f"模型: {settings.primary_model.model_name}")
    logger.info("=" * 60)

    uvicorn.run(
        "main:app",
        host=settings.server.host,
        port=settings.server.port,
        reload=settings.server.reload,
        log_level=settings.server.log_level,
    )
