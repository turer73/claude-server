"""RAG atomik reindex (#566) alias-swap güvenlik birimleri — Codex P2 regresyon koruması.

scripts/rag_index_all.py prosedürel bir script (import = tüm reindex'i çalıştırır), bu yüzden
güvenlik helper'larını (_collection_ready, _swap_alias) AST ile ÇIKARIP (kopya değil, dosyadaki
metnin ta kendisi) mock requests'le test ederiz — CI'da Qdrant gerektirmez, deterministik.

P2 (canlı koleksiyonu doğrulanmadan silme) iki kontrata dayanır:
  - _collection_ready(NEW, expected): NEW sorgu-hazır değilse False → prosedürel kod canlıya
    DOKUNMADAN abort eder (ready-check, first-migration delete'ten ÖNCE).
  - _swap_alias retry: silme sonrası geçici Qdrant hatası kalıcı kesintiye dönmez.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "rag_index_all.py"


class _Resp:
    def __init__(self, ok=True, status_code=200, payload=None, text=""):
        self.ok = ok
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class _FakeRequests:
    """get/post/delete'i kuyruğa-alınmış yanıtlarla veya exception'la besler."""

    def __init__(self):
        self.get_queue = []
        self.post_queue = []
        self.calls = {"get": 0, "post": 0, "delete": 0}

    def _next(self, queue, kind):
        self.calls[kind] += 1
        if not queue:
            return _Resp()
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, *a, **k):
        return self._next(self.get_queue, "get")

    def post(self, *a, **k):
        return self._next(self.post_queue, "post")

    def delete(self, *a, **k):
        self.calls["delete"] += 1
        return _Resp()


def _load_funcs(fake):
    """rag_index_all.py'den güvenlik fonksiyonlarını AST-extract edip fake-requests'le exec et."""
    tree = ast.parse(SCRIPT.read_text())
    ns = {
        "requests": fake,
        "time": type("_T", (), {"sleep": staticmethod(lambda *_: None)})(),  # sleep=no-op
        "sys": __import__("sys"),
        "QDRANT": "http://qdrant.test",
    }
    wanted = {"_collection_ready", "_swap_alias"}
    found = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            exec(compile(ast.Module([node], []), str(SCRIPT), "exec"), ns)  # noqa: S102
            found.add(node.name)
    assert wanted <= found, f"eksik fonksiyon: {wanted - found}"
    return ns


def _ready(green=True, points=5):
    return _Resp(payload={"result": {"status": "green" if green else "yellow", "points_count": points}})


# ── _collection_ready: 'doğrulanmış replacement' kontratı (P2 özü) ──


def test_collection_ready_rejects_nonpositive_expected():
    fake = _FakeRequests()
    fn = _load_funcs(fake)["_collection_ready"]
    # expected<=0: boş NEW ile swap = veri-kaybı → asla hazır, hiç istek atma.
    assert fn("klipper-memory-x", 0) is False
    assert fake.calls["get"] == 0


def test_collection_ready_false_when_not_green():
    fake = _FakeRequests()
    fake.get_queue = [_ready(green=False, points=99)]
    fn = _load_funcs(fake)["_collection_ready"]
    assert fn("c", 5, attempts=1) is False


def test_collection_ready_false_when_points_below_expected():
    fake = _FakeRequests()
    fake.get_queue = [_ready(green=True, points=3)]
    fn = _load_funcs(fake)["_collection_ready"]
    assert fn("c", 5, attempts=1) is False


def test_collection_ready_true_when_green_and_enough_points():
    fake = _FakeRequests()
    fake.get_queue = [_ready(green=True, points=5)]
    fn = _load_funcs(fake)["_collection_ready"]
    assert fn("c", 5, attempts=1) is True


def test_collection_ready_retries_until_indexed():
    # İlk poll henüz hazır değil (indexleme bitmemiş), ikincide hazır → retry kazanır.
    fake = _FakeRequests()
    fake.get_queue = [_ready(green=True, points=2), _ready(green=True, points=5)]
    fn = _load_funcs(fake)["_collection_ready"]
    assert fn("c", 5, attempts=3) is True
    assert fake.calls["get"] == 2


def test_collection_ready_false_on_request_exception():
    fake = _FakeRequests()
    fake.get_queue = [RuntimeError("qdrant down")]
    fn = _load_funcs(fake)["_collection_ready"]
    assert fn("c", 5, attempts=1) is False


# ── _swap_alias: silme-sonrası retry (kalıcı kesinti önleme) ──


def test_swap_alias_success_single_call():
    fake = _FakeRequests()
    fake.post_queue = [_Resp(ok=True)]
    fn = _load_funcs(fake)["_swap_alias"]
    assert fn([{"create_alias": {}}]) is True
    assert fake.calls["post"] == 1


def test_swap_alias_recovers_on_transient_then_succeeds():
    fake = _FakeRequests()
    fake.post_queue = [_Resp(ok=False, status_code=503, text="busy"), _Resp(ok=True)]
    fn = _load_funcs(fake)["_swap_alias"]
    assert fn([{"create_alias": {}}], attempts=4) is True
    assert fake.calls["post"] == 2


def test_swap_alias_fails_after_exhausting_retries():
    fake = _FakeRequests()
    fake.post_queue = [_Resp(ok=False, status_code=500, text="err")] * 4
    fn = _load_funcs(fake)["_swap_alias"]
    assert fn([{"create_alias": {}}], attempts=4) is False
    assert fake.calls["post"] == 4


def test_swap_alias_handles_exception_as_failure():
    fake = _FakeRequests()
    fake.post_queue = [ConnectionError("reset")] * 2
    fn = _load_funcs(fake)["_swap_alias"]
    assert fn([{"create_alias": {}}], attempts=2) is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
