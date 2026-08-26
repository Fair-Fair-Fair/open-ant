"""Zero-dependency BM25 keyword index with JSON persistence.

OpenClaw uses SQLite FTS5 for its keyword side; we keep the same idea
(keyword + vector dual index) but implement BM25 in pure Python so the
default install stays dependency-free.  The index interface is small on
purpose — a different backend (SQLite FTS5, Elasticsearch…) can replace
it without touching callers.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

_CJK_RUN = re.compile(r"[一-鿿㐀-䶿]+")
_ASCII_WORD = re.compile(r"[a-z0-9_]{2,}")

# Okapi BM25 constants (industry-standard defaults)
K1 = 1.5
B = 0.75


def tokenize(text: str) -> list[str]:
    """Mixed CJK/ASCII tokenization without external dependencies.

    - ASCII: lowercase alphanumeric words (len >= 2) — "Docker", "rag"
    - CJK: character unigrams + bigrams — standard Chinese approach when
      no segmenter (jieba) is installed; bigrams carry term precision,
      unigrams keep single-character queries (e.g. "蚁") recallable.
    """
    lowered = text.lower()
    tokens: list[str] = []
    tokens.extend(_ASCII_WORD.findall(lowered))
    for run_match in _CJK_RUN.finditer(text):
        run = run_match.group()
        tokens.extend(run)
        tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


class BM25Index:
    """In-memory BM25 index backed by a JSON file.

    Persistence stores only (doc_id -> tokens); postings/df/doc-lengths
    are rebuilt on load, keeping the file small and the format trivial.
    """

    def __init__(self, path: Path):
        self._path = Path(path)
        self._docs: dict[str, list[str]] = {}
        self._postings: dict[str, dict[str, int]] = {}   # term -> {doc_id: tf}
        self._df: dict[str, int] = {}                    # term -> doc freq
        self._doc_len: dict[str, int] = {}
        self._avgdl: float = 0.0
        self._dirty = False
        self._load()

    # ── CRUD ──

    def add(self, doc_id: str, text: str) -> None:
        """Index (or re-index) one document."""
        self.remove(doc_id)
        tokens = tokenize(text)
        if not tokens:
            return
        self._docs[doc_id] = tokens
        self._doc_len[doc_id] = len(tokens)
        for term in set(tokens):
            tf = tokens.count(term)
            self._postings.setdefault(term, {})[doc_id] = tf
            self._df[term] = self._df.get(term, 0) + 1
        n = len(self._docs)
        self._avgdl = (self._avgdl * (n - 1) + len(tokens)) / n
        self._dirty = True

    def remove(self, doc_id: str) -> None:
        """Remove one document from the index (no-op if absent)."""
        tokens = self._docs.pop(doc_id, None)
        if tokens is None:
            return
        old_len = self._doc_len.pop(doc_id, 0)
        n = len(self._docs)
        self._avgdl = (self._avgdl * (n + 1) - old_len) / n if n else 0.0
        for term in set(tokens):
            postings = self._postings.get(term)
            if not postings:
                continue
            postings.pop(doc_id, None)
            if postings:
                self._df[term] -= 1
            else:
                self._postings.pop(term, None)
                self._df.pop(term, None)
        self._dirty = True

    def search(self, query: str, top_k: int = 10) -> list[tuple[str, float]]:
        """Return [(doc_id, bm25_score)] sorted descending. Scores are raw
        BM25 values — not comparable across queries; the hybrid store
        normalizes/ranks them before fusion."""
        tokens = tokenize(query)
        if not tokens or not self._docs:
            return []

        n = len(self._docs)
        avgdl = self._avgdl or 1.0
        scores: dict[str, float] = {}

        for term in set(tokens):
            postings = self._postings.get(term)
            if not postings:
                continue
            idf = math.log(1 + (n - self._df[term] + 0.5) / (self._df[term] + 0.5))
            for doc_id, tf in postings.items():
                length = self._doc_len.get(doc_id, 0) or 1
                denom = tf + K1 * (1 - B + B * length / avgdl)
                scores[doc_id] = scores.get(doc_id, 0.0) + idf * tf * (K1 + 1) / denom

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        return ranked[:top_k]

    # ── Persistence ──

    def save(self) -> None:
        """Persist (doc_id -> tokens) to JSON. No-op when nothing changed."""
        if not self._dirty:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self._docs, ensure_ascii=False), encoding="utf-8"
        )
        tmp.replace(self._path)
        self._dirty = False

    def _load(self) -> None:
        """Load the persisted index; rebuild postings/df in memory."""
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return  # corrupt index file → start empty; hybrid store self-heals
        if not isinstance(raw, dict):
            return
        for doc_id, tokens in raw.items():
            if not isinstance(tokens, list):
                continue
            self._docs[doc_id] = tokens
            self._doc_len[doc_id] = len(tokens)
            for term in set(tokens):
                self._postings.setdefault(term, {})[doc_id] = tokens.count(term)
                self._df[term] = self._df.get(term, 0) + 1
        if self._docs:
            self._avgdl = sum(self._doc_len.values()) / len(self._docs)

    def __len__(self) -> int:
        return len(self._docs)
