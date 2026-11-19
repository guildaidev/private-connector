from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from guild_connector.memory import MemoryBudget, MemoryReader, MemorySnapshot

MIB = 1024 * 1024


class MemoryReaderTests(unittest.TestCase):
    def setUp(self):
        files = tempfile.TemporaryDirectory()
        self.addCleanup(files.cleanup)
        self.root = Path(files.name)

    def write(self, path, value):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(value))

    def test_v2_uses_leaf_limit_and_parent_available_memory(self):
        self.write("proc/self/cgroup", "0::/pod/connector\n")
        self.write(
            "proc/self/mountinfo", "1 0 0:1 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n"
        )
        for directory, limit, usage in (
            ("sys/fs/cgroup", "max", 0),
            ("sys/fs/cgroup/pod", 1024 * MIB, 900 * MIB),
            ("sys/fs/cgroup/pod/connector", 512 * MIB, 32 * MIB),
        ):
            self.write(directory + "/memory.max", limit)
            self.write(directory + "/memory.current", usage)
        reader = MemoryReader(self.root)
        self.assertEqual(reader.snapshot(), MemorySnapshot(512 * MIB, 124 * MIB))
        self.write("sys/fs/cgroup/pod/connector/memory.max", 256 * MIB)
        self.assertEqual(reader.snapshot(), MemorySnapshot(256 * MIB, 124 * MIB))

    def test_v2_namespace_root_uses_nonstandard_mount(self):
        self.write("proc/self/cgroup", "0::/\n")
        self.write(
            "proc/self/mountinfo",
            "1 0 0:1 /docker/connector /cgroup rw - cgroup2 cgroup rw\n",
        )
        self.write("cgroup/memory.max", 256 * MIB)
        self.write("cgroup/memory.current", 40 * MIB)
        self.assertEqual(
            MemoryReader(self.root).snapshot(), MemorySnapshot(256 * MIB, 216 * MIB)
        )

    def test_v1_uses_memory_controller_and_ignores_unlimited_sentinel(self):
        self.write("proc/self/cgroup", "5:cpu:/other\n6:memory:/connector\n")
        self.write(
            "proc/self/mountinfo",
            "1 0 0:1 / /sys/fs/cgroup/memory rw - cgroup cgroup rw,memory\n",
        )
        self.write("sys/fs/cgroup/memory/memory.limit_in_bytes", (1 << 63) - 4096)
        self.write("sys/fs/cgroup/memory/memory.usage_in_bytes", 0)
        self.write("sys/fs/cgroup/memory/connector/memory.limit_in_bytes", 512 * MIB)
        self.write("sys/fs/cgroup/memory/connector/memory.usage_in_bytes", 64 * MIB)
        self.assertEqual(
            MemoryReader(self.root).snapshot(), MemorySnapshot(512 * MIB, 448 * MIB)
        )

    def test_v1_hierarchical_limit_survives_a_hidden_parent(self):
        self.write("sys/fs/cgroup/memory/memory.limit_in_bytes", (1 << 63) - 4096)
        self.write("sys/fs/cgroup/memory/memory.usage_in_bytes", 32 * MIB)
        self.write(
            "sys/fs/cgroup/memory/memory.stat",
            f"hierarchical_memory_limit {256 * MIB}\n",
        )
        self.assertEqual(
            MemoryReader(self.root).snapshot(), MemorySnapshot(256 * MIB, 224 * MIB)
        )

    def test_unlimited_fallback_is_bounded_and_respects_host_pressure(self):
        self.write("sys/fs/cgroup/memory.max", "max")
        self.write("proc/meminfo", "MemTotal: 16777216 kB\nMemAvailable: 102400 kB\n")
        self.write("proc/self/statm", "100000 1000")
        snapshot = MemoryReader(self.root).snapshot()
        self.assertEqual(snapshot.limit, 512 * MIB)
        self.assertEqual(snapshot.available, 100 * MIB)
        self.write("proc/meminfo", "MemTotal: 262144 kB\nMemAvailable: 262144 kB\n")
        self.assertEqual(
            MemoryReader(self.root).snapshot(),
            MemorySnapshot(256 * MIB, 256 * MIB - 1000 * os.sysconf("SC_PAGE_SIZE")),
        )

    def test_missing_usage_or_malformed_limit_cannot_admit_work(self):
        self.write("sys/fs/cgroup/memory.max", 512 * MIB)
        reader = MemoryReader(self.root)
        self.assertIsNone(
            MemoryBudget(reader.snapshot).reserve_call(100, streaming=True)
        )
        self.write("sys/fs/cgroup/memory.max", "not-a-limit")
        self.write("sys/fs/cgroup/memory.current", 0)
        self.assertIsNone(
            MemoryBudget(reader.snapshot).reserve_call(100, streaming=True)
        )


class MemoryBudgetTests(unittest.TestCase):
    def test_concurrent_reservations_release_capacity_without_double_release(self):
        budget = MemoryBudget(lambda: MemorySnapshot(256 * MIB, 256 * MIB))
        first = budget.reserve(64 * MIB)
        second = budget.reserve(64 * MIB)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertIsNone(budget.reserve(1))
        first.release()
        first.release()
        replacement = budget.reserve(64 * MIB)
        self.assertIsNotNone(replacement)
        self.assertIsNone(budget.reserve(1))
        replacement.release()
        second.release()
        self.assertIsNotNone(budget.reserve(128 * MIB))

    def test_pressure_refuses_new_work_without_releasing_admitted_reservations(self):
        snapshot = MemorySnapshot(256 * MIB, 256 * MIB)
        budget = MemoryBudget(lambda: snapshot)
        first = budget.reserve_call(100, streaming=True)
        self.assertIsNotNone(first)
        snapshot = MemorySnapshot(256 * MIB, 0)
        self.assertIsNone(budget.reserve_call(100, streaming=True))
        snapshot = MemorySnapshot(256 * MIB, 256 * MIB)
        self.assertIsNone(budget.reserve(128 * MIB))
        first.release()
        self.assertIsNotNone(budget.reserve(128 * MIB))

    def test_unreadable_memory_state_refuses_new_work_and_recovers(self):
        readable = True

        def snapshot():
            if not readable:
                raise OSError("memory counter unavailable")
            return MemorySnapshot(256 * MIB, 256 * MIB)

        budget = MemoryBudget(snapshot)
        admitted = budget.reserve_call(100, streaming=True)
        self.assertIsNotNone(admitted)
        readable = False
        self.assertIsNone(budget.reserve_call(100, streaming=True))
        readable = True
        admitted.release()
        self.assertIsNotNone(budget.reserve(128 * MIB))
