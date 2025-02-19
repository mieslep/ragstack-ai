"""
This module provides an implementation of the BaseVectorStore abstract class, specifically designed
for use with a Cassandra database backend. It allows for the efficient storage and management of text embeddings
generated by a ColBERT model, facilitating scalable and high-relevancy retrieval operations.
"""

import asyncio
import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

import cassio
from cassandra.cluster import Session
from cassio.table.query import Predicate, PredicateOperator
from cassio.table.tables import ClusteredMetadataVectorCassandraTable

from .base_database import BaseDatabase
from .constant import DEFAULT_COLBERT_DIM
from .objects import Chunk, Vector


class CassandraDatabase(BaseDatabase):
    """
    An implementation of the BaseDatabase abstract base class using Cassandra as the backend
    storage system. This class provides methods to store, retrieve, and manage text embeddings within
    a Cassandra database, specifically designed for handling vector embeddings generated by ColBERT.

    The table schema and custom index for ANN queries are automatically created if they do not exist.
    """

    _table: ClusteredMetadataVectorCassandraTable

    def __new__(cls):
        raise ValueError(
            "This class cannot be instantiated directly. Please use the `from_astra()` or `from_session()` class methods."
        )

    @classmethod
    def from_astra(
        cls,
        database_id: str,
        astra_token: str,
        keyspace: Optional[str] = "default_keyspace",
        table_name: Optional[str] = "colbert",
        timeout: Optional[int] = 300,
    ):
        cassio.init(token=astra_token, database_id=database_id, keyspace=keyspace)
        session = cassio.config.resolve_session()
        session.default_timeout = timeout

        return cls.from_session(
            session=session, keyspace=keyspace, table_name=table_name
        )

    @classmethod
    def from_session(
        cls,
        session: Session,
        keyspace: Optional[str] = "default_keyspace",
        table_name: Optional[str] = "colbert",
    ):
        instance = super().__new__(cls)
        instance._initialize(session=session, keyspace=keyspace, table_name=table_name)
        return instance

    def _initialize(
        self,
        session: Session,
        keyspace: str,
        table_name: str,
    ):
        """
        Initializes a new instance of the CassandraVectorStore.

        Parameters:
            session (Session): The Cassandra session to use.
            keyspace (str): The keyspace in which the table exists or will be created.
            table_name (str): The name of the table to use or create for storing embeddings.
            timeout (int, optional): The default timeout in seconds for Cassandra operations. Defaults to 180.
        """

        try:
            is_astra = session.cluster.cloud
        except:
            is_astra = False

        logging.info(
            f"Cassandra store is running on {'AstraDB' if is_astra else 'Apache Cassandra'}."
        )

        self._table = ClusteredMetadataVectorCassandraTable(
            session=session,
            keyspace=keyspace,
            table=table_name,
            row_id_type=["INT", "INT"],
            vector_dimension=DEFAULT_COLBERT_DIM,
            vector_source_model="bert" if is_astra else None,
            vector_similarity_function=None if is_astra else "DOT_PRODUCT",
        )

    def _log_insert_error(self, doc_id: str, chunk_id: int, embedding_id: int, exp: Exception):
        if embedding_id == -1:
            logging.error(
                f"issue inserting document data: {doc_id} chunk: {chunk_id}: {exp}"
            )
        else:
            logging.error(
                f"issue inserting document embedding: {doc_id} chunk: {chunk_id} embedding: {embedding_id}: {exp}"
            )

    def add_chunks(self, chunks: List[Chunk]) -> List[Tuple[str, int]]:
        """
        Stores a list of embedded text chunks in the vector store

        Parameters:
            chunks (List[Chunk]): A list of `Chunk` instances to be stored.

        Returns:
            a list of tuples: (doc_id, chunk_id)
        """

        failed_chunks: List[Tuple[str, int]] = []
        success_chunks: List[Tuple[str, int]] = []

        for chunk in chunks:
            doc_id = chunk.doc_id
            chunk_id = chunk.chunk_id

            try:
                self._table.put(
                    partition_id=doc_id,
                    row_id=(chunk_id, -1),
                    body_blob=chunk.text,
                    metadata=chunk.metadata,
                )
            except Exception as exp:
                self._log_insert_error(doc_id=doc_id, chunk_id=chunk_id, embedding_id=-1, exp=exp)
                failed_chunks.append((doc_id, chunk_id))
                continue


            for embedding_id, vector in enumerate(chunk.embedding):
                try:
                    self._table.put(
                        partition_id=doc_id,
                        row_id=(chunk_id, embedding_id),
                        vector=vector,
                    )
                except Exception as exp:
                    self._log_insert_error(doc_id=doc_id, chunk_id=chunk_id, embedding_id=-1, exp=exp)
                    failed_chunks.append((doc_id, chunk_id))
                    continue

            success_chunks.append((doc_id, chunk_id))

        if len(failed_chunks) > 0:
            raise Exception(f"add failed for these chunks: {failed_chunks}. See error logs for more info.")

        return success_chunks

    async def _limited_put(
            self,
            sem: asyncio.Semaphore,
            doc_id: str,
            chunk_id: int,
            embedding_id: Optional[int] = -1,
            text: Optional[str] = None,
            metadata: Optional[Dict[str, Any]] = None,
            vector: Optional[Vector] = None,
    ) -> Tuple[str, int, int, Exception]:
        row_id = (chunk_id, embedding_id)
        exp = None
        async with sem:
            try:
                if vector is None:
                    await self._table.aput(
                        partition_id=doc_id,
                        row_id=row_id,
                        body_blob=text,
                        metadata=metadata,
                    )
                else:
                    await self._table.aput(
                        partition_id=doc_id, row_id=row_id, vector=vector
                    )
            except Exception as e:
                exp = e
            finally:
                return doc_id, chunk_id, embedding_id, exp

    async def aadd_chunks(self, chunks: List[Chunk], concurrent_inserts: Optional[int] = 100) -> List[Tuple[str, int]]:
        """
        Stores a list of embedded text chunks in the vector store

        Parameters:
            chunks (List[Chunk]): A list of `Chunk` instances to be stored.
            concurrent_inserts (Optional[int]): How many concurrent inserts to make to the database. Defaults to 100.

        Returns:
            a list of tuples: (doc_id, chunk_id)
        """
        semaphore = asyncio.Semaphore(concurrent_inserts)
        all_tasks = []
        tasks_per_chunk = defaultdict(int)

        for chunk in chunks:
            doc_id = chunk.doc_id
            chunk_id = chunk.chunk_id
            text = chunk.text
            metadata = chunk.metadata

            all_tasks.append(self._limited_put(
                sem=semaphore,
                doc_id=doc_id,
                chunk_id=chunk_id,
                text=text,
                metadata=metadata,
            ))
            tasks_per_chunk[(doc_id, chunk_id)] += 1


            for index, vector in enumerate(chunk.embedding):
                all_tasks.append(self._limited_put(
                    sem=semaphore,
                    doc_id=doc_id,
                    chunk_id=chunk_id,
                    embedding_id=index,
                    vector=vector,
                ))
                tasks_per_chunk[(doc_id, chunk_id)] += 1

        results = await asyncio.gather(*all_tasks, return_exceptions=True)

        for (doc_id, chunk_id, embedding_id, exp) in results:
            if exp is None:
                tasks_per_chunk[(doc_id, chunk_id)] -= 1
            else:
                self._log_insert_error(doc_id=doc_id, chunk_id=chunk_id, embedding_id=embedding_id, exp=exp)

        outputs: List[Tuple[str, int]] = []
        failed_chunks: List[Tuple[str, int]] = []

        for (doc_id, chunk_id) in tasks_per_chunk:
            if tasks_per_chunk[(doc_id, chunk_id)] == 0:
                outputs.append((doc_id, chunk_id))
            else:
                failed_chunks.append((doc_id, chunk_id))

        if len(failed_chunks) > 0:
            raise Exception(f"add failed for these chunks: {failed_chunks}. See error logs for more info.")

        return outputs

    def delete_chunks(self, doc_ids: List[str]) -> bool:
        """
        Deletes chunks from the vector store based on their document id.

        Parameters:
            doc_ids (List[str]): A list of document identifiers specifying the chunks to be deleted.

        Returns:
            True if the all the deletes were successful.
        """

        failed_docs: List[str] = []

        for doc_id in doc_ids:
            try:
                self._table.delete_partition(partition_id=doc_id)
            except Exception as exp:
                logging.error(f"issue on delete of document: {doc_id}: {exp}")
                failed_docs.append(doc_id)

        if len(failed_docs) > 0:
            raise Exception(f"delete failed for these docs: {failed_docs}. See error logs for more info.")

        return True

    async def _limited_delete(
            self,
            sem: asyncio.Semaphore,
            doc_id: str,
    ) -> Tuple[str, Exception]:
        exp = None
        async with sem:
            try:
                await self._table.adelete_partition(partition_id=doc_id)
            except Exception as e:
                exp = e
            finally:
                return doc_id, exp

    async def adelete_chunks(self, doc_ids: List[str], concurrent_deletes: Optional[int] = 100) -> bool:
        """
        Deletes chunks from the vector store based on their document id.

        Parameters:
            doc_ids (List[str]): A list of document identifiers specifying the chunks to be deleted.
            concurrent_deletes (Optional[int]): How many concurrent deletes to make to the database. Defaults to 100.

        Returns:
            True if the all the deletes were successful.
        """

        semaphore = asyncio.Semaphore(concurrent_deletes)
        all_tasks = []

        for doc_id in doc_ids:
            all_tasks.append(self._limited_delete(
                    sem=semaphore,
                    doc_id=doc_id,
                ))

        results = await asyncio.gather(*all_tasks, return_exceptions=True)

        success = True
        failed_docs: List[str] = []

        for (doc_id, exp) in results:
            if exp is not None:
                logging.error(
                    f"issue deleting document: {doc_id}: {exp}"
                )
                success = False
                failed_docs.append(doc_id)

        if len(failed_docs) > 0:
            raise Exception(f"delete failed for these docs: {failed_docs}. See error logs for more info.")

        return success

    async def search_relevant_chunks(self, vector: Vector, n: int) -> List[Chunk]:
        """
        Retrieves 'n' ANN results for an embedded token vector.

        Returns:
            A list of Chunks with only `doc_id` and `chunk_id` set.
            Fewer than 'n' results may be returned.
        """

        chunks: Set[Chunk] = set()

        # TODO: only return partition_id and row_id after cassio supports this
        rows = await self._table.aann_search(vector=vector, n=n)
        for row in rows:
            chunks.add(
                Chunk(
                    doc_id=row["partition_id"],
                    chunk_id=row["row_id"][0],
                )
            )
        return list(chunks)

    async def get_chunk_embedding(self, doc_id: str, chunk_id: int) -> Chunk:
        """
        Retrieve the embedding data for a chunk.

        Returns:
            A chunk with `doc_id`, `chunk_id`, and `embedding` set.
        """

        row_id = (chunk_id, Predicate(PredicateOperator.GT, -1))
        rows = await self._table.aget_partition(partition_id=doc_id, row_id=row_id)

        embedding = [row["vector"] for row in rows]

        return Chunk(doc_id=doc_id, chunk_id=chunk_id, embedding=embedding)

    async def get_chunk_data(
        self, doc_id: str, chunk_id: int, include_embedding: Optional[bool] = False
    ) -> Chunk:
        """
        Retrieve the text and metadata for a chunk.

        Returns:
            A chunk with `doc_id`, `chunk_id`, `text`, and `metadata` set.
        """

        row_id = (chunk_id, Predicate(PredicateOperator.EQ, -1))
        row = await self._table.aget(partition_id=doc_id, row_id=row_id)

        if include_embedding is True:
            embedded_chunk = await self.get_chunk_embedding(
                doc_id=doc_id, chunk_id=chunk_id
            )
            embedding = embedded_chunk.embedding
        else:
            embedding = None

        return Chunk(
            doc_id=doc_id,
            chunk_id=chunk_id,
            text=row["body_blob"],
            metadata=row["metadata"],
            embedding=embedding,
        )

    def close(self) -> None:
        """
        Cleans up any open resources.
        """
        pass
