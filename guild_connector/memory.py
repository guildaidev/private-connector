"""Automatic admission budgets for container memory and bounded call buffers."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from time import monotonic

from .protocol import MAX_FRAME_BYTES

_MIB = 1024 * 1024
_FALLBACK_LIMIT = 512 * _MIB
_UNLIMITED = 1 << 60
TRANSPORT_RESERVE = 4 * MAX_FRAME_BYTES
BUFFERED_RESERVE = 4 * MAX_FRAME_BYTES
STREAM_RESERVE = _MIB
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MemorySnapshot:
    limit: int
    available: int


class MemoryReader:
    def __init__(self, root: Path = Path("/")) -> None:
        self.root = root
        self.sources = self._sources()

    def _read(self, path: Path) -> str:
        try:
            return path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            return ""

    @staticmethod
    def _number(value: str) -> int | None:
        try:
            number = int(value)
        except ValueError:
            return None
        return number if 0 <= number < _UNLIMITED else None

    @staticmethod
    def _unescape(value: str) -> str:
        for escaped, literal in (
            (r"\040", " "),
            (r"\011", "\t"),
            (r"\012", "\n"),
            (r"\134", "\\"),
        ):
            value = value.replace(escaped, literal)
        return value

    def _sources(self) -> list[tuple[Path, Path]]:
        memberships = []
        for line in self._read(self.root / "proc/self/cgroup").splitlines():
            fields = line.split(":", 2)
            if len(fields) == 3:
                memberships.append((fields[1].split(","), PurePosixPath(fields[2])))
        sources: list[tuple[Path, Path]] = []
        for line in self._read(self.root / "proc/self/mountinfo").splitlines():
            before, separator, after = line.partition(" - ")
            mount, filesystem = before.split(), after.split()
            if not separator or len(mount) < 6 or len(filesystem) < 3:
                continue
            kind = filesystem[0]
            if kind != "cgroup2" and not (
                kind == "cgroup" and "memory" in filesystem[2].split(",")
            ):
                continue
            mount_root = PurePosixPath(self._unescape(mount[3]))
            mountpoint = self.root / self._unescape(mount[4]).lstrip("/")
            for controllers, membership in memberships:
                if (kind == "cgroup2" and controllers != [""]) or (
                    kind == "cgroup" and "memory" not in controllers
                ):
                    continue
                if not membership.is_absolute() or ".." in membership.parts:
                    continue
                try:
                    relative = membership.relative_to(mount_root)
                except ValueError:
                    if membership != PurePosixPath("/"):
                        continue
                    relative = PurePosixPath(".")
                directory = mountpoint / str(relative)
                while True:
                    names = (
                        ("memory.max", "memory.current")
                        if kind == "cgroup2"
                        else ("memory.limit_in_bytes", "memory.usage_in_bytes")
                    )
                    sources.append((directory / names[0], directory / names[1]))
                    if directory == mountpoint:
                        break
                    directory = directory.parent
        # Conventional mounts also work when proc mount metadata is unavailable.
        for directory, names in (
            ("sys/fs/cgroup", ("memory.max", "memory.current")),
            (
                "sys/fs/cgroup/memory",
                ("memory.limit_in_bytes", "memory.usage_in_bytes"),
            ),
        ):
            sources.append(
                (self.root / directory / names[0], self.root / directory / names[1])
            )
        return list(dict.fromkeys(sources))

    def snapshot(self) -> MemorySnapshot:
        constraints = []
        for limit_path, usage_path in self.sources:
            raw = self._read(limit_path)
            limit = self._number(raw)
            if limit_path.name == "memory.limit_in_bytes":
                for line in self._read(
                    limit_path.with_name("memory.stat")
                ).splitlines():
                    key, _, value = line.partition(" ")
                    inherited = (
                        self._number(value)
                        if key == "hierarchical_memory_limit"
                        else None
                    )
                    if inherited is not None:
                        limit = (
                            min(limit, inherited) if limit is not None else inherited
                        )
            if limit is None:
                unlimited = raw == "max" or (raw.isdigit() and int(raw) >= _UNLIMITED)
                if not unlimited:
                    try:
                        if limit_path.is_file():
                            constraints.append((0, 0))
                    except OSError:
                        constraints.append((0, 0))
                continue
            usage = self._number(self._read(usage_path))
            # An unreadable usage counter must not turn into free memory.
            constraints.append(
                (limit, max(0, limit - usage) if usage is not None else 0)
            )
        if constraints:
            return MemorySnapshot(
                min(limit for limit, _ in constraints),
                min(available for _, available in constraints),
            )
        info = {}
        for line in self._read(self.root / "proc/meminfo").splitlines():
            key, _, value = line.partition(":")
            fields = value.split()
            if fields and fields[0].isdigit():
                info[key] = int(fields[0]) * 1024
        limit = min(_FALLBACK_LIMIT, info.get("MemTotal", _FALLBACK_LIMIT))
        try:
            resident = int(
                self._read(self.root / "proc/self/statm").split()[1]
            ) * os.sysconf("SC_PAGE_SIZE")
        except (IndexError, ValueError, OSError):
            resident = limit
        available = min(max(0, limit - resident), info.get("MemAvailable", limit))
        return MemorySnapshot(limit, available)


class MemoryReservation:
    def __init__(self, budget: MemoryBudget, size: int) -> None:
        self.budget = budget
        self.size = size
        self.released = False

    def release(self) -> None:
        if not self.released:
            self.budget.reserved -= self.size
            self.released = True


class MemoryBudget:
    def __init__(self, snapshot: Callable[[], MemorySnapshot] | None = None) -> None:
        self.snapshot = snapshot or MemoryReader().snapshot
        self.reserved = 0
        self.last_warning = float("-inf")

    def _warn(self, message: str, *args: object) -> None:
        now = monotonic()
        if now - self.last_warning >= 30:
            log.warning(message, *args)
            self.last_warning = now

    def reserve(self, size: int) -> MemoryReservation | None:
        if size < 0:
            raise ValueError("Memory reservations cannot be negative")
        try:
            state = self.snapshot()
        except OSError:
            self._warn("Cannot read connector memory usage; rejecting new calls")
            return None
        headroom = max(32 * _MIB, state.limit // 4)
        available = max(
            0, min(state.limit, state.available) - headroom - TRANSPORT_RESERVE
        )
        if self.reserved + size > available:
            self._warn(
                "Connector memory budget is full; rejecting new calls (limit=%d available=%d reserved=%d)",
                state.limit,
                state.available,
                self.reserved,
            )
            return None
        self.reserved += size
        return MemoryReservation(self, size)

    def reserve_call(
        self, frame_size: int, *, streaming: bool
    ) -> MemoryReservation | None:
        working = STREAM_RESERVE if streaming else BUFFERED_RESERVE
        # Includes the decoded request body and retained strings/dictionaries.
        return self.reserve(working + frame_size * 4)
