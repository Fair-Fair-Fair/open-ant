"""Document ingestion pipeline: load → split → embed → store.

Phase 3A: embedding is batched via ``aembed`` (≤64 per call, one
exponential-backoff retry) and degrades **per chunk** — a single chunk
whose embedding fails is skipped with a warning while the rest of the
file keeps flowing (design principle 3/11).  Deterministic chunk ids are
preserved, so re-ingesting the same file is idempotent.
"""

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, List

# 引入各种文档加载器
from langchain_community.document_loaders import (
    CSVLoader,
    Docx2txtLoader,
    JSONLoader,
    PyPDFLoader,
    TextLoader,
    UnstructuredExcelLoader,
    UnstructuredHTMLLoader,
    UnstructuredMarkdownLoader,
    UnstructuredPowerPointLoader,
)
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

if TYPE_CHECKING:
    from ant.provider.memory.base import EmbeddingProvider, VectorStore

logger = logging.getLogger(__name__)

# Phase 3: max chunks per aembed call (dashscope batch limits), and the
# exponential-backoff delay for the single retry.
EMBED_BATCH_SIZE = 64
_EMBED_RETRY_BACKOFF_SECONDS = 1.0

# 扩展支持的文件扩展名
SUPPORTED_EXTENSIONS = {
    ".txt", ".md", ".csv", ".json", ".html", ".htm",
    ".py", ".js", ".ts", ".java", ".go", ".rs",
    ".yaml", ".yml", ".toml", ".xml", ".log",
    ".pdf", ".docx", ".pptx", ".xlsx", ".xls",
}


class DocumentIngester:
    """Loads documents, splits them into chunks with overlap, and stores in VectorStore."""

    def __init__(
        self,
        vector_store: "VectorStore",
        chunk_size: int = 500,
        chunk_overlap: int = 50,
        embedding_provider: "EmbeddingProvider | None" = None,
    ):
        """*embedding_provider* enables the Phase-3 batch-aembed + per-chunk
        degradation path; when omitted it is resolved from the store
        (``vector_store.embedding_provider``, or the wrapped store inside
        ``HybridMemoryStore``).  When none is available, ingestion falls
        back to the legacy store-internal embedding (e.g. fake stores)."""
        self.vector_store = vector_store
        self.embedding_provider = self._resolve_embedding_provider(
            vector_store, embedding_provider
        )
        self.splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            separators=[
                "\n\n", "\n", "。", ". ", "！", "! ", "？", "? ", "；", "; ", "，", ", ", " ",
            ],
            length_function=len,
        )
        logger.info(
            "DocumentIngester initialized: chunk_size=%d, chunk_overlap=%d",
            chunk_size, chunk_overlap,
        )

    def _load_document(self, path: Path) -> List[Document]:
        """Load a document using the appropriate LangChain loader based on file extension."""
        ext = path.suffix.lower()
        loader_map = {
            ".pdf": PyPDFLoader,
            ".docx": Docx2txtLoader,
            ".txt": TextLoader,
            ".md": UnstructuredMarkdownLoader,
            ".csv": CSVLoader,
            ".json": JSONLoader,
            ".html": UnstructuredHTMLLoader,
            ".htm": UnstructuredHTMLLoader,
            ".pptx": UnstructuredPowerPointLoader,
            ".xlsx": UnstructuredExcelLoader,
            ".xls": UnstructuredExcelLoader,
        }
        loader_cls = loader_map.get(ext)
        if not loader_cls:
            # 对于其他文本格式（如代码），尝试用 TextLoader 并指定 utf-8
            try:
                loader = TextLoader(str(path), encoding="utf-8")
            except Exception:
                raise ValueError(f"Unsupported file type: {ext} (supported: {SUPPORTED_EXTENSIONS})")  # noqa: E501
        else:
            loader = loader_cls(str(path))
        try:
            docs = loader.load()
        except Exception as e:
            logger.error("Failed to load document %s: %s", path, e)
            raise
        # 补充元数据（原始路径、文件名等）
        for doc in docs:
            if "source" not in doc.metadata:
                doc.metadata["source"] = str(path.resolve())
            doc.metadata["filename"] = path.name
            doc.metadata["extension"] = ext
        return docs

    async def ingest_file(self, file_path: str, extra_metadata: dict | None = None) -> int:
        """Load a single file, split into chunks, and store in vector DB.

        Returns the number of chunks stored.
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
        if not path.is_file():
            raise IsADirectoryError(f"Path is a directory: {file_path}")

        suffix = path.suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            raise ValueError(
                f"Unsupported file type: {suffix} (supported: {SUPPORTED_EXTENSIONS})"
            )

        # 使用加载器获取 Document 列表
        docs = self._load_document(path)

        if not docs:
            logger.warning("No content extracted from %s, skipping.", file_path)
            return 0

        # 添加额外元数据
        if extra_metadata:
            for doc in docs:
                doc.metadata.update(extra_metadata)

        # 分割文档（所有文档一起分割）
        split_docs = self.splitter.split_documents(docs)

        if not split_docs:
            logger.warning("No chunks generated from %s", file_path)
            return 0

        ids = []
        texts = []
        metadatas = []
        base_source = str(path.resolve())
        for i, chunk in enumerate(split_docs):
            chunk_id = self._make_deterministic_id(
                source=base_source,
                chunk_index=i,
                content=chunk.page_content,
            )
            ids.append(chunk_id)
            texts.append(chunk.page_content)
            # 合并元数据，添加 chunk_index 和 total_chunks
            meta = chunk.metadata.copy()
            meta["chunk_index"] = i
            meta["total_chunks"] = len(split_docs)
            meta["type"] = "document"  # 标记为文档片段
            metadatas.append(meta)

        # Phase 3: batch aembed (≤64, 1 exponential-backoff retry) with
        # chunk-level degradation — one bad chunk never sinks the file.
        stored = await self._store_chunks(texts, metadatas, ids)

        logger.info(
            "Ingested %s: %d/%d chunks stored (source=%s)",
            path.name, stored, len(split_docs), base_source,
        )
        return stored

    def _resolve_embedding_provider(
        self,
        vector_store: "VectorStore",
        explicit: "EmbeddingProvider | None",
    ) -> "EmbeddingProvider | None":
        """Locate the embedding provider used for batch embedding.

        Explicit argument wins; otherwise the store's own provider is
        reused (the HybridMemoryStore wraps the real store in ``_store``).
        """
        provider = explicit
        if provider is None:
            provider = getattr(vector_store, "embedding_provider", None)
        if provider is None:
            inner = getattr(vector_store, "_store", None)
            provider = getattr(inner, "embedding_provider", None) if inner is not None else None
        return provider

    async def _store_chunks(
        self,
        texts: List[str],
        metadatas: List[dict],
        ids: List[str],
    ) -> int:
        """Batch-embed then store; return the number of chunks stored.

        Chunks whose embedding fails are dropped with a warning (they are
        excluded from the store call), so one bad chunk cannot sink the
        rest of the file.  Without an embedding provider the legacy path
        (store embeds internally) is used unchanged.
        """
        aembed = getattr(self.embedding_provider, "aembed", None)
        if aembed is None:
            # 关键修正：参数名必须为 documents，而不是 texts
            await self.vector_store.add(documents=texts, metadatas=metadatas, ids=ids)
            return len(texts)

        vectors = await self._embed_batches(texts, aembed)
        survivors = [i for i, v in enumerate(vectors) if v is not None]
        dropped = len(texts) - len(survivors)
        if dropped:
            logger.warning("Dropped %d chunk(s) whose embedding failed", dropped)
        if not survivors:
            logger.error("All %d chunk(s) failed embedding — nothing stored", len(texts))
            return 0

        await self.vector_store.add(
            documents=[texts[i] for i in survivors],
            metadatas=[metadatas[i] for i in survivors],
            ids=[ids[i] for i in survivors],
        )
        return len(survivors)

    async def _embed_batches(
        self,
        texts: List[str],
        aembed,
    ) -> List[List[float] | None]:
        """Embed in batches (≤64); one vector per text, None = dropped.

        A batch that still fails after its retry degrades to per-chunk
        attempts so a single failing chunk cannot take the rest of the
        file down with it.
        """
        vectors: List[List[float] | None] = []
        for start in range(0, len(texts), EMBED_BATCH_SIZE):
            batch = texts[start : start + EMBED_BATCH_SIZE]
            batch_vectors = await self._embed_batch(aembed, batch)
            if batch_vectors is not None:
                vectors.extend(batch_vectors)
                continue
            for text in batch:
                try:
                    one = await self._embed_batch(aembed, [text])
                    vectors.append(one[0] if one else None)
                except Exception as e:  # noqa: BLE001 — per-chunk degradation
                    logger.warning(
                        "Embedding failed for chunk %r...: %s", text[:80], e
                    )
                    vectors.append(None)
        return vectors

    @staticmethod
    async def _embed_batch(aembed, batch: List[str]) -> List[List[float]] | None:
        """One batch attempt with a single exponential-backoff retry.

        Returns None when the batch failed after retries (caller decides
        per-chunk degradation).
        """
        delay = _EMBED_RETRY_BACKOFF_SECONDS
        for attempt in range(2):
            try:
                return await aembed(batch)
            except Exception as e:  # noqa: BLE001
                if attempt == 0:
                    logger.warning(
                        "Embedding batch of %d chunk(s) failed (%s); retrying in %.1fs",
                        len(batch), e, delay,
                    )
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                logger.warning(
                    "Embedding batch of %d chunk(s) failed after retry: %s",
                    len(batch), e,
                )
                return None
        return None  # pragma: no cover — loop always returns

    async def ingest_directory(self, dir_path: str, extra_metadata: dict | None = None) -> int:
        """Recursively ingest all supported files in a directory.

        Returns total number of chunks stored.
        """
        root = Path(dir_path)
        if not root.is_dir():
            raise NotADirectoryError(f"Not a directory: {dir_path}")

        total = 0
        for file_path in sorted(root.rglob("*")):
            if file_path.is_file() and file_path.suffix.lower() in SUPPORTED_EXTENSIONS:
                try:
                    count = await self.ingest_file(str(file_path), extra_metadata)
                    total += count
                except Exception as e:
                    logger.warning("Failed to ingest %s: %s", file_path, e)

        logger.info("Directory ingest complete: %d total chunks from %s", total, dir_path)
        return total

    async def delete_by_source(self, source: str) -> int:
        """Delete all chunks belonging to a specific source file.

        Returns the number of chunks deleted (0 if no match).

        Phase 3 (Qdrant): ``delete_by_filter`` deletes by payload filter
        in one server-side step — no query-then-delete, no residue.
        Legacy Chroma keeps the get(where=...)-then-delete path.
        """
        deleter = getattr(self.vector_store, "delete_by_filter", None)
        if deleter is not None:
            return await deleter({"source": source})

        # 直接用 Chroma collection 的 where 过滤按 source 取回全部 chunk id，
        # 修复旧实现 query(top_k=1) 只删第一块、其余 chunk 永远残留的问题。
        collection = getattr(self.vector_store, "_collection", None)
        if collection is None:
            logger.warning(
                "Vector store does not expose a Chroma collection; "
                "skip source delete for %s",
                source,
            )
            return 0

        result = collection.get(where={"source": source})
        ids_to_delete = list(result.get("ids") or []) if result else []

        if not ids_to_delete:
            logger.info("No chunks found for source: %s", source)
            return 0

        await self.vector_store.delete(ids_to_delete)
        logger.info("Deleted %d chunks for source: %s", len(ids_to_delete), source)
        return len(ids_to_delete)

    @staticmethod
    def _make_deterministic_id(source: str, chunk_index: int, content: str) -> str:
        """Generate a deterministic ID from source + chunk_index + content hash."""
        raw = f"{source}::{chunk_index}::{content}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]
