"""向量库的两种模式选择。

服务端模式（QDRANT_URL 有值）是部署态；嵌入式只保留给本机开发与测试。这里的
断言不需要真的 Qdrant 服务：QdrantClient 的构造是惰性的，而"是否动用本地目录"
就是区分两种模式的行为差异。
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def isolated_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from app.config import get_settings
    from app.retrieval import vector_store

    settings = get_settings()
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data", raising=False)
    vector_store.get_client.cache_clear()
    yield settings, vector_store
    vector_store.get_client.cache_clear()


def test_server_mode_does_not_touch_local_storage(isolated_store, monkeypatch) -> None:
    settings, vector_store = isolated_store
    # 必须走 monkeypatch：get_settings() 是进程内单例，直接赋值会把
    # qdrant_url 泄漏给后续所有用例（症状是它们全部连不上向量库）。
    monkeypatch.setattr(settings, "qdrant_url", "http://qdrant:6333")

    vector_store.get_client()

    # 服务端模式下不得在本地建 qdrant 目录：一旦建了，说明又回到了"单进程独占"
    # 的嵌入式语义，部署态（多实例/脚本并存）会重新撞锁。
    assert not (settings.data_dir / "qdrant").exists()


def test_embedded_mode_creates_local_storage(isolated_store, monkeypatch) -> None:
    settings, vector_store = isolated_store
    monkeypatch.setattr(settings, "qdrant_url", "")

    vector_store.get_client()

    assert (settings.data_dir / "qdrant").is_dir()
