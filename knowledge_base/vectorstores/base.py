from abc import ABC, abstractmethod
from typing import Dict, List


class VectorStore(ABC):
    @abstractmethod
    def health(self) -> Dict:
        raise NotImplementedError

    @abstractmethod
    def upsert(self, points: List[Dict]) -> None:
        raise NotImplementedError

    @abstractmethod
    def search(self, vector: List[float], filters: Dict, limit: int) -> List[Dict]:
        raise NotImplementedError

    @abstractmethod
    def delete(self, filters: Dict) -> None:
        raise NotImplementedError

    @abstractmethod
    def count(self, filters: Dict) -> int:
        raise NotImplementedError

    def optimize(self) -> None:
        """Compact and index the store when the backend supports it."""
