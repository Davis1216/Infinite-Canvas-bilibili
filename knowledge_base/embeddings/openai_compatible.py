from typing import Dict, List

import httpx

from .base import EmbeddingProvider


class OpenAICompatibleEmbeddingProvider(EmbeddingProvider):
    def __init__(self, profile: Dict):
        self.profile = profile
        self.base_url = str(profile.get("base_url") or "").rstrip("/")
        self.api_key = str(profile.get("api_key") or "")
        self.model = str(profile.get("model") or "")
        self._dimensions = int(profile.get("dimensions") or 0)
        self.timeout = float(profile.get("timeout_seconds") or 60)

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        if not self.base_url or not self.model or self._dimensions <= 0:
            raise RuntimeError("嵌入模型尚未配置完整")
        endpoint = self.base_url if self.base_url.endswith("/embeddings") else f"{self.base_url}/embeddings"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = {"model": self.model, "input": texts, "dimensions": self._dimensions}
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(endpoint, headers=headers, json=body)
        if response.status_code >= 400:
            detail = response.text[:500]
            raise RuntimeError(f"嵌入接口请求失败（{response.status_code}）：{detail}")
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or len(data) != len(texts):
            raise RuntimeError("嵌入接口返回数量与输入不一致")
        ordered = sorted(data, key=lambda item: int(item.get("index", 0)))
        vectors = [item.get("embedding") for item in ordered]
        for vector in vectors:
            if not isinstance(vector, list) or len(vector) != self._dimensions:
                actual = len(vector) if isinstance(vector, list) else 0
                raise RuntimeError(f"嵌入维度不匹配：配置 {self._dimensions}，实际 {actual}")
        return vectors
