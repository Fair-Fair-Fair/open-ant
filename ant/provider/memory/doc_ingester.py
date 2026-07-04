"""Document ingestion pipeline: load → split → embed → store."""

import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, List

from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_core.documents import Document

# 引入各种文档加载器
from langchain_community.document_loaders import (
    PyPDFLoader,
    Docx2txtLoader,
    TextLoader,
    CSVLoader,
    JSONLoader,
    UnstructuredMarkdownLoader,
    UnstructuredHTMLLoader,
    UnstructuredPowerPointLoader,
    UnstructuredExcelLoader,
)

if TYPE_CHECKING:
    from ant.provider.memory.base import VectorStore

logger = logging.getLogger(__name__)

# 扩展支持的文件扩展名
SUPPORTED_EXTENSIONS = {
    ".txt", ".md", ".csv", ".json", ".html", ".htm",
    ".py", ".js", ".ts", ".java", ".go", ".rs",
    ".yaml", ".yml", ".toml", ".xml", ".log",
    ".pdf", ".docx", ".pptx", ".xlsx", ".xls",
}


class DocumentIngester:
    """Loads documents, splits them into chunks with overlap, and stores in VectorStore."""

    def __init__(self, vector_store: "VectorStore", chunk_size: int = 1000, chunk_overlap: int = 200):
        self.vector_store = vector_store
        self.splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            separators=["\n\n", "\n", "。", ". ", "！", "! ", "？", "? ", "；", "; ", "，", ", ", " "],
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
                raise ValueError(f"Unsupported file type: {ext} (supported: {SUPPORTED_EXTENSIONS})")
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
            raise ValueError(f"Unsupported file type: {suffix} (supported: {SUPPORTED_EXTENSIONS})")

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

        # 关键修正：参数名必须为 documents，而不是 texts
        await self.vector_store.add(documents=texts, metadatas=metadatas, ids=ids)

        logger.info(
            "Ingested %s: %d chunks stored (source=%s)",
            path.name, len(split_docs), base_source,
        )
        return len(split_docs)

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

    async def delete_by_source(self, source: str) -> None:
        """Delete all chunks belonging to a specific source file."""
        # 注意：本方法需要根据 source 精确删除，当前实现通过 query 获取所有文档再过滤，效率较低。
        # 更好的做法是直接使用 ChromaDB 的 where 过滤，但当前接口未支持，可后续优化。
        # 此处保留原实现，但可能因数据量大而不佳。
        results = await self.vector_store.query(query_text="", top_k=1)
        all_docs = await self.vector_store.get(ids=[d.id for d in results]) if results else []

        ids_to_delete = [
            doc.id for doc in all_docs
            if doc.metadata.get("source") == source
        ]
        if ids_to_delete:
            await self.vector_store.delete(ids_to_delete)
            logger.info("Deleted %d chunks for source: %s", len(ids_to_delete), source)

    @staticmethod
    def _make_deterministic_id(source: str, chunk_index: int, content: str) -> str:
        """Generate a deterministic ID from source + chunk_index + content hash."""
        raw = f"{source}::{chunk_index}::{content}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]
