"""JourneyPilot 维护命令入口。

    uv run python journeypilot.py doctor
    uv run python journeypilot.py migrate
    uv run python journeypilot.py backup --label before-upgrade
    uv run python journeypilot.py restore backups/backup-… --yes

与 `main.py` 同一套 `sys.path` 引导（这个仓不作为包安装，见 `[tool.uv] package = false`），
所以两个入口看到的是同一份 `travel_agent`。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from travel_agent.cli.main import main

if __name__ == "__main__":
    raise SystemExit(main())
