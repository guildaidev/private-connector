from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from guild_connector.health import healthy, report_health


class HealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnected_is_live_but_not_ready_and_shutdown_removes_health(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "health.json"
            self.assertFalse(healthy(path))
            stop = asyncio.Event()
            task = asyncio.create_task(
                report_health(path, SimpleNamespace(connected=False), stop)
            )
            await asyncio.sleep(0)
            self.assertTrue(healthy(path))
            self.assertFalse(healthy(path, ready=True))
            stop.set()
            await task
            self.assertFalse(healthy(path))

    async def test_connected_is_ready(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "health.json"
            stop = asyncio.Event()
            task = asyncio.create_task(
                report_health(path, SimpleNamespace(connected=True), stop)
            )
            await asyncio.sleep(0)
            self.assertTrue(healthy(path, ready=True))
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.assertFalse(healthy(path))

    def test_missing_corrupt_stale_and_future_state_fail_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "health.json"
            for content in (
                "bad json",
                "[]",
                "null",
                "{}",
                json.dumps({"updated": time.monotonic() - 31, "connected": True}),
                json.dumps({"updated": time.monotonic() + 60, "connected": True}),
            ):
                with self.subTest(content=content):
                    path.write_text(content)
                    self.assertFalse(healthy(path))
