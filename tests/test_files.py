import pytest
import os
from app.core.file_manager import FileManager
from app.exceptions import AuthorizationError, NotFoundError


@pytest.fixture
def fm(tmp_path):
    return FileManager(allowed_paths=[str(tmp_path)], max_file_size_mb=10)


def test_validate_path_allowed(fm, tmp_path):
    path = str(tmp_path / "test.txt")
    assert fm.validate_path(path) == path


def test_validate_path_traversal_blocked(fm):
    with pytest.raises(AuthorizationError, match="not in allowed paths"):
        fm.validate_path("/etc/shadow")


def test_validate_path_traversal_dotdot(fm, tmp_path):
    with pytest.raises(AuthorizationError):
        fm.validate_path(str(tmp_path / ".." / ".." / "etc" / "passwd"))


def test_read_file(fm, tmp_path):
    f = tmp_path / "hello.txt"
    f.write_text("line1\nline2\nline3\n")
    result = fm.read_file(str(f))
    assert result["content"] == "line1\nline2\nline3\n"
    assert result["lines"] == 3
    assert result["size"] > 0


def test_read_file_not_found(fm, tmp_path):
    with pytest.raises(NotFoundError):
        fm.read_file(str(tmp_path / "nonexistent.txt"))


def test_read_file_with_offset_limit(fm, tmp_path):
    f = tmp_path / "big.txt"
    f.write_text("\n".join(f"line{i}" for i in range(100)))
    result = fm.read_file(str(f), offset=10, limit=5)
    lines = result["content"].strip().split("\n")
    assert len(lines) == 5
    assert lines[0] == "line10"


def test_write_file(fm, tmp_path):
    path = str(tmp_path / "new.txt")
    fm.write_file(path, "hello world")
    assert (tmp_path / "new.txt").read_text() == "hello world"


def test_write_file_append(fm, tmp_path):
    f = tmp_path / "append.txt"
    f.write_text("first\n")
    fm.write_file(str(f), "second\n", mode="append")
    assert f.read_text() == "first\nsecond\n"


def test_edit_file(fm, tmp_path):
    f = tmp_path / "edit.txt"
    f.write_text("hello world")
    fm.edit_file(str(f), "hello", "goodbye")
    assert f.read_text() == "goodbye world"


def test_delete_file(fm, tmp_path):
    f = tmp_path / "delete_me.txt"
    f.write_text("bye")
    fm.delete_file(str(f))
    assert not f.exists()


def test_list_directory(fm, tmp_path):
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    (tmp_path / "subdir").mkdir()
    entries = fm.list_directory(str(tmp_path))
    assert len(entries) >= 3
    names = [e["path"] for e in entries]
    assert any("a.txt" in n for n in names)


def test_file_info(fm, tmp_path):
    f = tmp_path / "info.txt"
    f.write_text("test content")
    info = fm.get_file_info(str(f))
    assert info["size"] > 0
    assert info["is_dir"] is False


def test_search_files(fm, tmp_path):
    (tmp_path / "match_this.py").write_text("x")
    (tmp_path / "other.txt").write_text("y")
    results = fm.search_files(str(tmp_path), pattern="*.py")
    assert len(results) >= 1
    assert any("match_this.py" in r for r in results)
