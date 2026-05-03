import pytest

from app.core.log_manager import LogManager


@pytest.fixture
def log_mgr(tmp_path):
    # Create sample log files
    log1 = tmp_path / "app.log"
    log1.write_text(
        "2026-03-27 10:00:00 INFO [agent] Started agent\n"
        "2026-03-27 10:00:01 ERROR [agent] Connection failed\n"
        "2026-03-27 10:00:02 INFO [agent] Retrying...\n"
        "2026-03-27 10:00:03 WARNING [monitor] CPU high\n"
        "2026-03-27 10:00:04 INFO [agent] Connected\n"
    )
    log2 = tmp_path / "monitor.log"
    log2.write_text("2026-03-27 10:00:00 INFO [monitor] Started monitoring\n2026-03-27 10:00:05 ERROR [monitor] Disk full\n")
    sources = {
        "agent": str(log1),
        "monitor": str(log2),
    }
    return LogManager(sources=sources)


def test_list_sources(log_mgr):
    sources = log_mgr.list_sources()
    assert "agent" in sources
    assert "monitor" in sources


def test_tail_default(log_mgr):
    lines = log_mgr.tail(source="agent", n=3)
    assert len(lines) == 3
    assert "Connected" in lines[-1]


def test_tail_all_sources(log_mgr):
    lines = log_mgr.tail(n=10)
    assert len(lines) == 7  # 5 + 2


def test_search_pattern(log_mgr):
    results = log_mgr.search("ERROR")
    assert len(results) == 2
    assert any("Connection failed" in r for r in results)
    assert any("Disk full" in r for r in results)


def test_search_with_source_filter(log_mgr):
    results = log_mgr.search("ERROR", source="agent")
    assert len(results) == 1
    assert "Connection failed" in results[0]


def test_search_no_match(log_mgr):
    results = log_mgr.search("CRITICAL")
    assert len(results) == 0


def test_search_regex(log_mgr):
    results = log_mgr.search(r"10:00:0[0-2]")
    assert len(results) >= 3


def test_stats(log_mgr):
    stats = log_mgr.stats()
    assert stats["total_lines"] == 7
    assert stats["sources"]["agent"] == 5
    assert stats["sources"]["monitor"] == 2


def test_stats_by_level(log_mgr):
    stats = log_mgr.stats()
    assert stats["levels"]["INFO"] >= 4
    assert stats["levels"]["ERROR"] == 2


def test_tail_empty_source(tmp_path):
    empty = tmp_path / "empty.log"
    empty.write_text("")
    mgr = LogManager(sources={"empty": str(empty)})
    lines = mgr.tail(source="empty", n=10)
    assert len(lines) == 0


def test_search_limit(log_mgr):
    results = log_mgr.search("INFO", limit=2)
    assert len(results) == 2
