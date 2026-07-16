from .base import VectorStore
from .lancedb import LanceDbVectorStore


def create_vector_store(profile):
    return LanceDbVectorStore(profile)


__all__ = ["VectorStore", "LanceDbVectorStore", "create_vector_store"]
