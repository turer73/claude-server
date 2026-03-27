"""RAG API — index documents and query with semantic search."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.rag_engine import RAGEngine
from app.middleware.dependencies import require_auth, require_write

router = APIRouter(prefix="/api/v1/rag", tags=["rag"])

_engine = RAGEngine()


class IndexTextRequest(BaseModel):
    text: str
    source: str = "manual"
    metadata: dict | None = None


class IndexFileRequest(BaseModel):
    path: str


class IndexDirRequest(BaseModel):
    directory: str
    pattern: str = "*.md"


class QueryRequest(BaseModel):
    question: str
    n_results: int = 5
    generate: bool = True


@router.post("/index/text")
async def index_text(req: IndexTextRequest, _: None = Depends(require_write)) -> dict:
    """Index raw text into the RAG store."""
    return await _engine.index_text(req.text, source=req.source, metadata=req.metadata)


@router.post("/index/file")
async def index_file(req: IndexFileRequest, _: None = Depends(require_write)) -> dict:
    """Index a file from the server filesystem."""
    return await _engine.index_file(req.path)


@router.post("/index/directory")
async def index_directory(req: IndexDirRequest, _: None = Depends(require_write)) -> dict:
    """Index all matching files in a directory."""
    return await _engine.index_directory(req.directory, pattern=req.pattern)


@router.post("/query")
async def query(req: QueryRequest, _: None = Depends(require_auth)) -> dict:
    """Query indexed documents with semantic search + optional LLM answer."""
    return await _engine.query(req.question, n_results=req.n_results, generate=req.generate)


@router.get("/stats")
async def stats(_: None = Depends(require_auth)) -> dict:
    """Get RAG collection statistics."""
    return await _engine.stats()


@router.delete("/collection")
async def delete_collection(_: None = Depends(require_write)) -> dict:
    """Delete the entire RAG collection."""
    return await _engine.delete_collection()
