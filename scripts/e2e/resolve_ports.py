#!/usr/bin/env python3
"""Discover live JourneyPilot API / app ports after run.sh fallback.

Resolution order:
  1. Env: JOURNEYPILOT_API_BASE / JOURNEYPILOT_APP_URL / BACKEND_PORT / FRONTEND_PORT
  2. Canonical runtime state: tmp/journeypilot.ports (written by ./run.sh start)
  3. JourneyPilot API scan: 127.0.0.1:8001–8020

Usage:
  python scripts/e2e/resolve_ports.py
  python scripts/e2e/resolve_ports.py --json
  from scripts.e2e.resolve_ports import resolve  # when run as module path on PYTHONPATH
"""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
PORTS_FILE = REPO_ROOT / "tmp" / "journeypilot.ports"


@dataclass(frozen=True)
class LivePorts:
    backend_port: int
    frontend_port: int
    api_base: str
    app_url: str
    source: str


def _parse_kv_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.is_file():
        return data
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        data[key.strip()] = val.strip().strip('"').strip("'")
    return data


def _read_json(url: str, timeout: float) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        if resp.status != 200:
            return {}
        body = json.loads(resp.read().decode("utf-8", errors="replace"))
        return body if isinstance(body, dict) else {}


def _journeypilot_api_ok(port: int, timeout: float = 0.6) -> bool:
    try:
        ready = _read_json(f"http://127.0.0.1:{port}/api/health/ready", timeout)
        if ready.get("status") != "ready":
            return False
        openapi = _read_json(f"http://127.0.0.1:{port}/openapi.json", timeout)
        return openapi.get("info", {}).get("title") == "JourneyPilot TripOps API"
    except (json.JSONDecodeError, urllib.error.URLError, TimeoutError, OSError):
        return False


def _scan_backend(start: int = 8001, end: int = 8020) -> Optional[int]:
    for port in range(start, end + 1):
        if _journeypilot_api_ok(port):
            return port
    return None


def resolve(
    *,
    prefer_backend: Optional[int] = None,
    prefer_frontend: Optional[int] = None,
) -> LivePorts:
    # 1) env
    env_api = (os.environ.get("JOURNEYPILOT_API_BASE") or os.environ.get("E2E_API_BASE") or "").strip()
    env_app = (os.environ.get("JOURNEYPILOT_APP_URL") or os.environ.get("E2E_APP_URL") or "").strip()
    env_bp = os.environ.get("BACKEND_PORT") or os.environ.get("SERVER_PORT")
    env_fp = os.environ.get("FRONTEND_PORT")

    if env_api:
        m = re.search(r":(\d+)", env_api)
        bp = int(m.group(1)) if m else int(env_bp or 8001)
        fp = int(env_fp or prefer_frontend or 8080)
        app = env_app or f"http://127.0.0.1:{fp}"
        return LivePorts(bp, fp, env_api.rstrip("/"), app.rstrip("/"), "env")

    if env_bp:
        bp = int(env_bp)
        fp = int(env_fp or prefer_frontend or 8080)
        return LivePorts(
            bp,
            fp,
            f"http://127.0.0.1:{bp}/api",
            f"http://127.0.0.1:{fp}",
            "env_port",
        )

    # 2) canonical runtime state
    kv = _parse_kv_file(PORTS_FILE)
    if "BACKEND_PORT" in kv:
        bp = int(kv["BACKEND_PORT"])
        fp = int(env_fp or kv.get("FRONTEND_PORT") or prefer_frontend or 8080)
        api = kv.get("API_BASE") or f"http://127.0.0.1:{bp}/api"
        app = env_app or kv.get("APP_URL") or f"http://127.0.0.1:{fp}"
        if _journeypilot_api_ok(bp):
            return LivePorts(bp, fp, api.rstrip("/"), app.rstrip("/"), "runtime_state")

    # 3) health + API identity scan
    if prefer_backend and _journeypilot_api_ok(prefer_backend):
        bp = prefer_backend
        source = "prefer"
    else:
        scanned = _scan_backend()
        if scanned is None:
            raise RuntimeError(
                "No live JourneyPilot API found (env, tmp/journeypilot.ports, "
                "or :8001–8020 API scan). Start with ./run.sh start"
            )
        bp = scanned
        source = "health_scan"

    fp = prefer_frontend or int(env_fp or 8080)
    # soft-detect frontend if ports file missing
    if not env_fp:
        for candidate in range(8080, 8091):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{candidate}/", timeout=0.4) as resp:
                    if resp.status == 200:
                        fp = candidate
                        break
            except (urllib.error.URLError, TimeoutError, OSError):
                continue

    return LivePorts(
        bp,
        fp,
        f"http://127.0.0.1:{bp}/api",
        f"http://127.0.0.1:{fp}",
        source,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print JSON")
    args = parser.parse_args()
    ports = resolve()
    if args.json:
        print(json.dumps(asdict(ports), ensure_ascii=False, indent=2))
    else:
        print(f"source={ports.source}")
        print(f"BACKEND_PORT={ports.backend_port}")
        print(f"FRONTEND_PORT={ports.frontend_port}")
        print(f"API_BASE={ports.api_base}")
        print(f"APP_URL={ports.app_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
