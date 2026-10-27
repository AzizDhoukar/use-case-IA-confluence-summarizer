"""Local embeddings with sentence-transformers (no external service), stored in SQLite."""

from __future__ import annotations

import logging
from typing import Protocol

import numpy as np

from .db import Database
from .text import chunk_text

log = logging.getLogger(__name__)


class Encoder(Protocol):
    name: str

    def encode(self, texts: list[str]) -> np.ndarray:
        """Return one L2-normalized float32 vector per text."""


class SentenceTransformerEncoder:
    """Loads the model lazily; long texts are embedded as the mean of model-sized windows,
    because small models only see their first max_seq_length tokens (128 for MiniLM)."""

    def __init__(self, name: str, batch_size: int = 32):
        self.name = name
        self.batch_size = batch_size
        self._model = None

    @property
    def model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer  # heavy import
            log.info("Loading embedding model %s", self.name)
            self._model = SentenceTransformer(self.name, device="cpu")
        return self._model

    def encode(self, texts: list[str]) -> np.ndarray:
        window = max(200, int(self.model.max_seq_length * 3))  # ~3 chars per subword token
        windows, owners = [], []
        for i, text in enumerate(texts):
            for start in range(0, max(len(text), 1), window):
                windows.append(text[start:start + window])
                owners.append(i)
        vecs = self.model.encode(windows, batch_size=self.batch_size, convert_to_numpy=True,
                                 normalize_embeddings=True, show_progress_bar=False)
        out = np.zeros((len(texts), vecs.shape[1]), dtype=np.float32)
        np.add.at(out, np.array(owners), vecs)
        return normalize(out)


def normalize(m: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(m, axis=-1, keepdims=True)
    return (m / np.where(norms == 0, 1, norms)).astype(np.float32)


def to_blob(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def ensure_embeddings(db: Database, encoder: Encoder, pages: list, chunk_tokens: int) -> int:
    """(Re)build chunks + embeddings for pages whose content or model changed. Returns count."""
    todo = []
    for page in pages:
        row = db.one("SELECT content_hash, model FROM chunks WHERE page_id = ? LIMIT 1",
                     (page["id"],))
        if not row or row["content_hash"] != page["content_hash"] or row["model"] != encoder.name:
            todo.append(page)
    if not todo:
        return 0
    log.info("Embedding %d page(s) with %s", len(todo), encoder.name)
    for start in range(0, len(todo), 64):
        batch = todo[start:start + 64]
        items = []
        for page in batch:
            for ch in chunk_text(page["body_text"], chunk_tokens):
                items.append((page, ch, f"{page['title']}\n{ch.heading}\n{ch.text}"))
        vecs = encoder.encode([t for _, _, t in items]) if items else []
        for page in batch:
            db.execute("DELETE FROM chunks WHERE page_id = ?", (page["id"],))
        for (page, ch, _), vec in zip(items, vecs):
            db.execute("INSERT INTO chunks(page_id, idx, heading, text, content_hash, model, "
                       "embedding) VALUES(?,?,?,?,?,?,?)",
                       (page["id"], ch.idx, ch.heading, ch.text, page["content_hash"],
                        encoder.name, to_blob(vec)))
        db.commit()
    return len(todo)


def page_vectors(db: Database, page_ids: list[str], model: str) -> tuple[list[str], np.ndarray]:
    """One vector per page: normalized mean of its chunk vectors. Pages without chunks are dropped."""
    sums: dict[str, np.ndarray] = {}
    wanted = set(page_ids)
    for r in db.query("SELECT page_id, embedding FROM chunks WHERE model = ?", (model,)):
        if r["page_id"] in wanted:
            v = from_blob(r["embedding"])
            sums[r["page_id"]] = sums[r["page_id"]] + v if r["page_id"] in sums else v.copy()
    ids = [pid for pid in page_ids if pid in sums]
    if not ids:
        return [], np.zeros((0, 0), dtype=np.float32)
    return ids, normalize(np.stack([sums[pid] for pid in ids]))
