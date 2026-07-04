"""ChromaDB vector store implementation."""

import logging
from typing import TYPE_CHECKING

import chromadb

from .base import MemoryDocument, VectorStore, EmbeddingProvider

if TYPE_CHECKING:
    from ant.utils.config import Config

logger = logging.getLogger(__name__)

COLLECTION_NAME = "ant_memory"


class ChromaVectorStore(VectorStore):
    """Vector store backed by ChromaDB with local persistence."""

    def __init__(self, config: "Config", embedding_provider: EmbeddingProvider):
        self.embedding_provider = embedding_provider
        persist_dir = str(config.memory.persist_directory)
        self._client = chromadb.PersistentClient(path=persist_dir)
        self._collection = self._client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info(
            f"ChromaDB initialized at {persist_dir}, "
            f"collection '{COLLECTION_NAME}' has {self._collection.count()} documents"
        )

    async def add(
        self,
        documents: list[str],
        metadatas: list[dict] | None = None,
        ids: list[str] | None = None,
    ) -> None:
        """Add documents to ChromaDB with embeddings."""
        if not documents:
            return

        if ids is None:
            import uuid
            ids = [str(uuid.uuid4()) for _ in documents]

        # Generate embeddings
        embeddings = await self.embedding_provider.embed(documents)

        self._collection.upsert(
            documents=documents,
            metadatas=metadatas,
            ids=ids,
            embeddings=embeddings,
        )
        logger.debug(f"Added {len(documents)} documents to ChromaDB")

    async def delete(self, ids: list[str]) -> None:
        """Delete documents by ids."""
        if not ids:
            return
        self._collection.delete(ids=ids)
        logger.debug(f"Deleted {len(ids)} documents from ChromaDB")

    async def get(self, ids: list[str]) -> list[MemoryDocument]:
        """Retrieve documents by ids."""
        if not ids:
            return []
        results = self._collection.get(ids=ids)
        if not results or not results["ids"]:
            return []
        docs = []
        for i, doc_id in enumerate(results["ids"]):
            content = results["documents"][i] if results["documents"] else ""
            meta = results["metadatas"][i] if results["metadatas"] else {}
            docs.append(MemoryDocument(id=doc_id, content=content, metadata=meta))
        return docs

    async def update(
            self,
            id: str,
            document: str,
            metadata: dict,
    ) -> None:
        """Update existing memory."""
        # Generate embedding for the new document
        embeddings = await self.embedding_provider.embed([document])
        self._collection.upsert(
            ids=[id],
            documents=[document],
            metadatas=[metadata],
            embeddings=embeddings,
        )

    async def query(
        self, query_text: str, top_k: int = 5
    ) -> list[MemoryDocument]:
        """Query ChromaDB for similar documents."""
        if self._collection.count() == 0:
            return []

        k = min(top_k, self._collection.count())
        # Generate query embedding
        query_embedding = await self.embedding_provider.embed([query_text])
        results = self._collection.query(
            query_embeddings=query_embedding,
            n_results=k,
        )

        memories: list[MemoryDocument] = []
        if results and results["documents"]:
            docs = results["documents"][0]
            ids = results["ids"][0]
            metas = results["metadatas"][0] if results["metadatas"] else [{}] * len(docs)
            distances = results["distances"][0] if results["distances"] else [0.0] * len(docs)

            for doc, id_, meta, dist in zip(
                    docs,
                    ids,
                    metas,
                    distances,
            ):
                memories.append(
                    MemoryDocument(
                        id=id_,
                        content=doc,
                        metadata=meta or {},
                        score=1.0 - dist,
                    )
                )

        return memories
