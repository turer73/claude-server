"""Unit tests for RAGEngine — chunk_text, embed, index, query with mocked HTTP."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.rag_engine import RAGEngine


@pytest.fixture
def engine():
    return RAGEngine(
        chroma_url="http://fake-chroma:8100",
        ollama_url="http://fake-ollama:11434",
    )


def test_chunk_text_basic(engine):
    text = " ".join(["word"] * 100)
    chunks = engine._chunk_text(text, chunk_size=50, overlap=10)
    assert len(chunks) >= 2
    assert all(len(c.split()) <= 50 for c in chunks)


def test_chunk_text_empty(engine):
    assert engine._chunk_text("") == []
    assert engine._chunk_text("   ") == []


def test_chunk_text_small(engine):
    chunks = engine._chunk_text("hello world", chunk_size=500)
    assert len(chunks) == 1
    assert chunks[0] == "hello world"


def test_chunk_text_overlap(engine):
    words = [f"w{i}" for i in range(20)]
    text = " ".join(words)
    chunks = engine._chunk_text(text, chunk_size=10, overlap=3)
    assert len(chunks) >= 2
    # Check overlap: last words of first chunk appear in second
    first_words = set(chunks[0].split()[-3:])
    second_words = set(chunks[1].split()[:3])
    assert first_words & second_words  # intersection should exist


@pytest.mark.anyio
async def test_embed(engine):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"embeddings": [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]}

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
        result = await engine._embed(["hello", "world"])
        assert len(result) == 2
        assert result[0] == [0.1, 0.2, 0.3]


@pytest.mark.anyio
async def test_embed_empty_response(engine):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {}

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
        result = await engine._embed(["test"])
        assert result == []


@pytest.mark.anyio
async def test_ensure_collection_existing(engine):
    mock_resp = MagicMock()
    mock_resp.json.return_value = [{"name": "documents", "id": "abc-123"}]

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
        col_id = await engine._ensure_collection()
        assert col_id == "abc-123"


@pytest.mark.anyio
async def test_ensure_collection_create(engine):
    mock_get = MagicMock()
    mock_get.json.return_value = []  # no existing collections

    mock_post = MagicMock()
    mock_post.json.return_value = {"id": "new-123"}

    with (
        patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_get),
        patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_post),
    ):
        col_id = await engine._ensure_collection()
        assert col_id == "new-123"


@pytest.mark.anyio
async def test_index_text_empty(engine):
    with patch.object(engine, "_ensure_collection", new_callable=AsyncMock, return_value="col-1"):
        result = await engine.index_text("", source="test")
        assert result["indexed"] == 0


@pytest.mark.anyio
async def test_index_text_success(engine):
    with (
        patch.object(engine, "_ensure_collection", new_callable=AsyncMock, return_value="col-1"),
        patch.object(engine, "_embed", new_callable=AsyncMock, return_value=[[0.1, 0.2]]),
    ):
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
            result = await engine.index_text("hello world test", source="test")
            assert result["indexed"] == 1
            assert result["source"] == "test"


@pytest.mark.anyio
async def test_index_file_not_found(engine):
    from app.exceptions import ServerError

    with pytest.raises(ServerError, match="File not found"):
        await engine.index_file("/nonexistent/path.txt")


@pytest.mark.anyio
async def test_index_file_too_large(engine, tmp_path):
    from app.exceptions import ServerError

    big_file = tmp_path / "big.txt"
    big_file.write_text("x" * (11 * 1024 * 1024))
    with pytest.raises(ServerError, match="too large"):
        await engine.index_file(str(big_file))


@pytest.mark.anyio
async def test_index_file_success(engine, tmp_path):
    test_file = tmp_path / "doc.txt"
    test_file.write_text("This is a test document with enough words to create a chunk")

    with patch.object(engine, "index_text", new_callable=AsyncMock, return_value={"indexed": 1, "source": "doc.txt"}):
        result = await engine.index_file(str(test_file))
        assert result["indexed"] == 1


@pytest.mark.anyio
async def test_index_directory(engine, tmp_path):
    (tmp_path / "a.md").write_text("file a content")
    (tmp_path / "b.md").write_text("file b content")
    (tmp_path / "c.txt").write_text("not matched")

    with patch.object(engine, "index_file", new_callable=AsyncMock, return_value={"indexed": 1, "source": "x"}):
        result = await engine.index_directory(str(tmp_path), pattern="*.md")
        assert result["files"] == 2
        assert result["chunks"] == 2


@pytest.mark.anyio
async def test_index_directory_not_found(engine):
    from app.exceptions import ServerError

    with pytest.raises(ServerError, match="Directory not found"):
        await engine.index_directory("/nonexistent/dir")


@pytest.mark.anyio
async def test_query_search_only(engine):
    mock_query_resp = MagicMock()
    mock_query_resp.json.return_value = {
        "documents": [["doc1", "doc2"]],
        "metadatas": [[{"source": "a.md"}, {"source": "b.md"}]],
        "distances": [[0.1, 0.5]],
    }

    with (
        patch.object(engine, "_ensure_collection", new_callable=AsyncMock, return_value="col-1"),
        patch.object(engine, "_embed", new_callable=AsyncMock, return_value=[[0.1, 0.2]]),
        patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_query_resp),
    ):
        result = await engine.query("what is linux?", n_results=2, generate=False)
        assert result["question"] == "what is linux?"
        assert len(result["results"]) == 2
        assert result["results"][0]["distance"] == 0.1


@pytest.mark.anyio
async def test_stats(engine):
    mock_resp = MagicMock()
    mock_resp.json.return_value = 42

    with (
        patch.object(engine, "_ensure_collection", new_callable=AsyncMock, return_value="col-1"),
        patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp),
    ):
        result = await engine.stats()
        assert result["document_count"] == 42


@pytest.mark.anyio
async def test_delete_collection(engine):
    mock_resp = MagicMock()
    mock_resp.status_code = 200

    with patch("httpx.AsyncClient.delete", new_callable=AsyncMock, return_value=mock_resp):
        result = await engine.delete_collection()
        assert result["deleted"] == "documents"
