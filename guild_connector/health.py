"""File-based probes without an inbound listener or identity access."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from .identity import atomic_write
from .tunnel_client import TunnelClient


async def report_health(path: Path, tunnel: TunnelClient, stop: asyncio.Event) -> None:
    try:
        while not stop.is_set():
            atomic_write(
                path,
                json.dumps(
                    {"updated": time.monotonic(), "connected": tunnel.connected}
                ),
            )
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except TimeoutError:
                pass
    finally:
        path.unlink(missing_ok=True)


def healthy(path: Path, *, ready: bool = False) -> bool:
    try:
        status = json.loads(path.read_text())
        age = time.monotonic() - status["updated"]
        return 0 <= age < 30 and (not ready or status["connected"] is True)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state-file", type=Path, default=Path("/tmp/connector-health.json")
    )
    parser.add_argument("--ready", action="store_true")
    args = parser.parse_args()
    raise SystemExit(0 if healthy(args.state_file, ready=args.ready) else 1)
