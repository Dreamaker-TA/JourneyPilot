#!/usr/bin/env python3
"""Review or explicitly publish release-managed JourneyPilot product data.

Usage:
    uv run python scripts/publish_product_configuration.py
    uv run python scripts/publish_product_configuration.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Sequence

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))
sys.path.insert(0, str(ROOT_DIR))

from travel_agent.preset.publication import build_publication_summary, publish_seed


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate or explicitly publish JourneyPilot system product configuration."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the reviewed Python seed to product_configurations and system travel_presets",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    summary = asyncio.run(publish_seed()) if args.apply else build_publication_summary()
    result = {
        "mode": "apply" if args.apply else "dry_run",
        "publication": summary,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
