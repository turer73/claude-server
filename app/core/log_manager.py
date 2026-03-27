"""Log manager — aggregation, search, tail across multiple log sources."""

from __future__ import annotations

import os
import re
from collections import Counter


class LogManager:
    """Aggregate and search logs from multiple sources."""

    def __init__(self, sources: dict[str, str] | None = None) -> None:
        self._sources = sources or {}

    def add_source(self, name: str, path: str) -> None:
        self._sources[name] = path

    def list_sources(self) -> list[str]:
        return list(self._sources.keys())

    def _read_lines(self, source: str | None = None) -> list[str]:
        lines = []
        sources = {source: self._sources[source]} if source and source in self._sources else self._sources
        for name, path in sources.items():
            if not os.path.isfile(path):
                continue
            try:
                with open(path, "r", errors="replace") as f:
                    for line in f:
                        stripped = line.rstrip("\n")
                        if stripped:
                            lines.append(stripped)
            except (OSError, PermissionError):
                continue
        return lines

    def tail(self, source: str | None = None, n: int = 50) -> list[str]:
        lines = self._read_lines(source)
        return lines[-n:]

    def search(
        self,
        pattern: str,
        source: str | None = None,
        limit: int = 100,
    ) -> list[str]:
        lines = self._read_lines(source)
        try:
            regex = re.compile(pattern)
        except re.error:
            regex = re.compile(re.escape(pattern))

        results = []
        for line in lines:
            if regex.search(line):
                results.append(line)
                if len(results) >= limit:
                    break
        return results

    def stats(self) -> dict:
        total = 0
        source_counts: dict[str, int] = {}
        level_counts: Counter = Counter()

        for name, path in self._sources.items():
            if not os.path.isfile(path):
                source_counts[name] = 0
                continue
            count = 0
            try:
                with open(path, "r", errors="replace") as f:
                    for line in f:
                        stripped = line.strip()
                        if not stripped:
                            continue
                        count += 1
                        for level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
                            if level in stripped:
                                level_counts[level] += 1
                                break
            except (OSError, PermissionError):
                pass
            source_counts[name] = count
            total += count

        return {
            "total_lines": total,
            "sources": source_counts,
            "levels": dict(level_counts),
        }
