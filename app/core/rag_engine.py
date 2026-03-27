"""RAG engine — index documents and query with semantic search + LLM."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import httpx

from app.exceptions import ServerError


class RAGEngine:
    """Retrieval-Augmented Generation using ChromaDB + Ollama embeddings."""

    def __init__(
        self,
        chroma_url: str = "http://localhost:8000",
        ollama_url: str = "http://localhost:11434",
        embed_model: str = "nomic-embed-text",
        chat_model: str = "qwen3:1.7b",
        collection: str = "documents",
    ) -> None:
        self._chroma = chroma_url
        self._ollama = ollama_url
        self._embed_model = embed_model
        self._chat_model = chat_model
        self._collection = collection
        self._tenant = "default_tenant"
        self._database = "default_database"

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings via Ollama."""
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{self._ollama}/api/embed",
                json={"model": self._embed_model, "input": texts},
            )
        data = resp.json()
        return data.get("embeddings", [])

    async def _ensure_collection(self) -> str:
        """Create collection if not exists, return its id."""
        url = f"{self._chroma}/api/v2/tenants/{self._tenant}/databases/{self._database}/collections"
        async with httpx.AsyncClient(timeout=10) as client:
            # Check existing
            resp = await client.get(url)
            for col in resp.json():
                if col.get("name") == self._collection:
                    return col["id"]
            # Create
            resp = await client.post(
                url,
                json={"name": self._collection, "metadata": {"description": "RAG document store"}},
            )
            return resp.json()["id"]

    def _chunk_text(self, text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
        """Split text into overlapping chunks."""
        words = text.split()
        chunks = []
        for i in range(0, len(words), chunk_size - overlap):
            chunk = " ".join(words[i : i + chunk_size])
            if chunk.strip():
                chunks.append(chunk)
        return chunks

    async def index_text(self, text: str, source: str = "unknown", metadata: dict | None = None) -> dict:
        """Index a text document into ChromaDB."""
        col_id = await self._ensure_collection()
        chunks = self._chunk_text(text)
        if not chunks:
            return {"indexed": 0, "source": source}

        embeddings = await self._embed(chunks)
        if len(embeddings) != len(chunks):
            raise ServerError("Embedding count mismatch")

        ids = [hashlib.md5(f"{source}:{i}".encode()).hexdigest() for i in range(len(chunks))]
        metadatas = [{"source": source, "chunk": i, **(metadata or {})} for i in range(len(chunks))]

        url = f"{self._chroma}/api/v2/tenants/{self._tenant}/databases/{self._database}/collections/{col_id}/add"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                url,
                json={
                    "ids": ids,
                    "embeddings": embeddings,
                    "documents": chunks,
                    "metadatas": metadatas,
                },
            )
        if resp.status_code >= 400:
            raise ServerError(f"ChromaDB add failed: {resp.text}")

        return {"indexed": len(chunks), "source": source}

    async def index_file(self, path: str) -> dict:
        """Read and index a file."""
        p = Path(path)
        if not p.exists():
            raise ServerError(f"File not found: {path}")
        if p.stat().st_size > 10 * 1024 * 1024:
            raise ServerError("File too large (max 10MB)")

        text = p.read_text(errors="replace")
        return await self.index_text(text, source=str(p.name))

    async def index_directory(self, directory: str, pattern: str = "*.md") -> dict:
        """Index all matching files in a directory."""
        p = Path(directory)
        if not p.is_dir():
            raise ServerError(f"Directory not found: {directory}")

        total = 0
        files = 0
        for f in p.rglob(pattern):
            if f.is_file() and f.stat().st_size < 10 * 1024 * 1024:
                try:
                    result = await self.index_file(str(f))
                    total += result["indexed"]
                    files += 1
                except Exception:
                    continue
        return {"files": files, "chunks": total, "directory": directory, "pattern": pattern}

    async def query(self, question: str, n_results: int = 5, generate: bool = True) -> dict:
        """Search for relevant chunks and optionally generate an answer."""
        col_id = await self._ensure_collection()
        q_embedding = await self._embed([question])
        if not q_embedding:
            raise ServerError("Failed to embed question")

        url = f"{self._chroma}/api/v2/tenants/{self._tenant}/databases/{self._database}/collections/{col_id}/query"
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                url,
                json={
                    "query_embeddings": q_embedding,
                    "n_results": n_results,
                    "include": ["documents", "metadatas", "distances"],
                },
            )
        data = resp.json()

        docs = data.get("documents", [[]])[0]
        metas = data.get("metadatas", [[]])[0]
        distances = data.get("distances", [[]])[0]

        results = [
            {"text": doc, "source": meta.get("source", "?"), "distance": round(dist, 4)}
            for doc, meta, dist in zip(docs, metas, distances)
        ]

        if not generate:
            return {"question": question, "results": results}

        # Build context for LLM
        context = "\n\n".join([f"[{r['source']}]: {r['text']}" for r in results])
        prompt = f"""Based on the following context, answer the question. If the context doesn't contain enough information, say so.

Context:
{context}

Question: {question}

Answer:"""

        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(
                f"{self._ollama}/api/chat",
                json={
                    "model": self._chat_model,
                    "messages": [{"role": "user", "content": f"/no_think {prompt}"}],
                    "stream": False,
                },
            )
        ai_data = resp.json()
        answer = ai_data.get("message", {}).get("content", "")

        return {
            "question": question,
            "answer": answer,
            "sources": results,
        }

    async def stats(self) -> dict:
        """Get collection statistics."""
        col_id = await self._ensure_collection()
        url = f"{self._chroma}/api/v2/tenants/{self._tenant}/databases/{self._database}/collections/{col_id}/count"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
        return {"collection": self._collection, "document_count": resp.json()}

    async def delete_collection(self) -> dict:
        """Delete the entire collection."""
        url = f"{self._chroma}/api/v2/tenants/{self._tenant}/databases/{self._database}/collections/{self._collection}"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.delete(url)
        return {"deleted": self._collection, "status": resp.status_code}
