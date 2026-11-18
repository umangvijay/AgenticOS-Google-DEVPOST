"""Legacy ABC kept for the orchestrator's type contract.

Implementations live in repositories/sqlite/memory_repository.py (SQLite,
cosine similarity in Python) and repositories/firestore/memory_repository.py
(Firestore native vector search). The in-memory and mock variants that used
to live here were removed — nothing demo or placeholder ships in this tree.
"""

from abc import ABC, abstractmethod
from typing import List

from backend.models.memory import MemoryEntry, MemorySearchResult


class MemoryRepository(ABC):
    @abstractmethod
    def store_memory(self, user_id: str, content: str, metadata: dict, embedding: List[float]) -> str:
        pass

    @abstractmethod
    def search_memory(self, user_id: str, query_embedding: List[float], limit: int = 5) -> List[MemorySearchResult]:
        pass
