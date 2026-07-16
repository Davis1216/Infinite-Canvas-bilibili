import math
import re
import threading
from pathlib import Path
from typing import Dict, List, Optional

from ..config import settings
from .base import VectorStore


_LOCKS: Dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _optional_dependencies():
    try:
        import lancedb
        import pyarrow as pa
    except ImportError as exc:
        raise RuntimeError("本地向量组件 LanceDB 尚未安装，请重新运行项目依赖安装脚本") from exc
    return lancedb, pa


def _sql_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _where(filters: Dict) -> Optional[str]:
    clauses = []
    for key, value in filters.items():
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", str(key)):
            raise ValueError(f"不安全的向量过滤字段：{key}")
        if isinstance(value, list):
            if not value:
                return "false"
            clauses.append(f"{key} IN ({','.join(_sql_value(item) for item in value)})")
        else:
            clauses.append(f"{key} = {_sql_value(value)}")
    return " AND ".join(clauses) if clauses else None


class LanceDbVectorStore(VectorStore):
    """Embedded, disk-backed vector storage. No server or external database is required."""

    def __init__(self, profile: Dict, vector_dir: Path = settings.vector_dir):
        self.profile = dict(profile or {})
        self.profile_id = str(self.profile.get("id") or "default")
        self.dimensions = int(self.profile.get("dimensions") or 0)
        if self.dimensions < 1:
            raise ValueError("嵌入配置缺少有效的向量维度")
        self.vector_dir = Path(vector_dir)
        safe_id = re.sub(r"[^a-zA-Z0-9_]", "_", self.profile_id)[:80]
        self.table_name = f"profile_{safe_id}"
        lock_key = str(self.vector_dir.resolve()) + ":" + self.table_name
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(lock_key, threading.RLock())

    def _connect(self):
        lancedb, _ = _optional_dependencies()
        self.vector_dir.mkdir(parents=True, exist_ok=True)
        return lancedb.connect(str(self.vector_dir))

    def _table_names(self, database) -> List[str]:
        result = database.list_tables()
        if hasattr(result, "tables"):
            return list(result.tables)
        return list(result)

    def _open(self, create: bool = False):
        database = self._connect()
        if self.table_name in self._table_names(database):
            table = database.open_table(self.table_name)
            vector_type = table.schema.field("vector").type
            actual = getattr(vector_type, "list_size", None)
            if actual is not None and int(actual) != self.dimensions:
                raise RuntimeError(f"本地向量表维度为 {actual}，嵌入配置维度为 {self.dimensions}，请重建索引")
            return table
        if not create:
            return None
        _, pa = _optional_dependencies()
        schema = pa.schema([
            pa.field("id", pa.string(), nullable=False),
            pa.field("chunk_id", pa.string(), nullable=False),
            pa.field("user_id", pa.string(), nullable=False),
            pa.field("knowledge_base_id", pa.string(), nullable=False),
            pa.field("document_id", pa.string(), nullable=False),
            pa.field("version_id", pa.string(), nullable=False),
            pa.field("generation", pa.int64(), nullable=False),
            pa.field("vector", pa.list_(pa.float32(), self.dimensions), nullable=False),
        ])
        return database.create_table(self.table_name, schema=schema)

    @staticmethod
    def _validate_vector(vector, dimensions: int) -> List[float]:
        if not isinstance(vector, (list, tuple)) or len(vector) != dimensions:
            actual = len(vector) if isinstance(vector, (list, tuple)) else 0
            raise ValueError(f"嵌入接口返回 {actual} 维向量，但配置要求 {dimensions} 维")
        result = [float(value) for value in vector]
        if not all(math.isfinite(value) for value in result):
            raise ValueError("嵌入接口返回了无效数值")
        return result

    def health(self) -> Dict:
        try:
            with self._lock:
                table = self._open(create=False)
                count = table.count_rows() if table is not None else 0
            return {
                "ok": True,
                "backend": "lancedb",
                "embedded": True,
                "path": str(self.vector_dir.resolve()),
                "table": self.table_name,
                "dimensions": self.dimensions,
                "vector_count": int(count),
            }
        except Exception as exc:
            return {
                "ok": False,
                "backend": "lancedb",
                "embedded": True,
                "path": str(self.vector_dir.resolve()),
                "table": self.table_name,
                "dimensions": self.dimensions,
                "vector_count": 0,
                "error": str(exc),
            }

    def upsert(self, points: List[Dict]) -> None:
        if not points:
            return
        rows = []
        for point in points:
            payload = point.get("payload") or {}
            rows.append({
                "id": str(point.get("id") or payload.get("chunk_id") or ""),
                "chunk_id": str(payload.get("chunk_id") or point.get("id") or ""),
                "user_id": str(payload.get("user_id") or ""),
                "knowledge_base_id": str(payload.get("knowledge_base_id") or ""),
                "document_id": str(payload.get("document_id") or ""),
                "version_id": str(payload.get("version_id") or ""),
                "generation": int(payload.get("generation") or 1),
                "vector": self._validate_vector(point.get("vector"), self.dimensions),
            })
        if any(not row["id"] or not row["chunk_id"] for row in rows):
            raise ValueError("向量数据缺少文本块标识")
        with self._lock:
            table = self._open(create=True)
            table.delete(f"id IN ({','.join(_sql_value(row['id']) for row in rows)})")
            table.add(rows)

    def search(self, vector: List[float], filters: Dict, limit: int) -> List[Dict]:
        query_vector = self._validate_vector(vector, self.dimensions)
        with self._lock:
            table = self._open(create=False)
            if table is None:
                return []
            query = table.search(query_vector).metric("cosine")
            clause = _where(filters)
            if clause:
                query = query.where(clause)
            rows = query.limit(max(1, int(limit))).to_list()
        result = []
        for row in rows:
            distance = float(row.get("_distance") or 0.0)
            payload = {key: row.get(key) for key in (
                "chunk_id", "user_id", "knowledge_base_id", "document_id", "version_id", "generation"
            )}
            result.append({"id": str(row.get("id")), "score": max(-1.0, min(1.0, 1.0 - distance)), "payload": payload})
        return result

    def delete(self, filters: Dict) -> None:
        with self._lock:
            table = self._open(create=False)
            clause = _where(filters)
            if table is not None and clause:
                table.delete(clause)

    def count(self, filters: Dict) -> int:
        with self._lock:
            table = self._open(create=False)
            if table is None:
                return 0
            clause = _where(filters)
            return int(table.count_rows(clause) if clause else table.count_rows())

    def optimize(self) -> None:
        with self._lock:
            table = self._open(create=False)
            if table is None:
                return
            count = int(table.count_rows())
            if count >= settings.vector_index_threshold and not table.list_indices():
                # Scalar quantization accepts arbitrary embedding dimensions;
                # product quantization requires dimensions divisible by its
                # sub-vector count and would reject otherwise valid models.
                table.create_index(metric="cosine", index_type="IVF_HNSW_SQ", replace=True)
            table.optimize()
