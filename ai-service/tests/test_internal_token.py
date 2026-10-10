"""内部契约的共享密钥门槛。

Java 侧是 fail-closed（8000 对外，未配置即拒绝启动）；这里在配置了令牌时逐请求
校验，未配置时只打警告——所以本文件的重点是把"配置了就一定拦得住"钉死。
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import INTERNAL_TOKEN_HEADER, create_app

TOKEN = "test-internal-token"


@pytest.fixture()
def app_with_token(monkeypatch: pytest.MonkeyPatch):
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "internal_token", TOKEN, raising=False)
    return create_app()


@pytest.mark.anyio
async def test_v1_requires_the_token(app_with_token) -> None:
    transport = ASGITransport(app=app_with_token)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        missing = await client.get("/v1/tools")
        assert missing.status_code == 401

        wrong = await client.get("/v1/tools", headers={INTERNAL_TOKEN_HEADER: "nope"})
        assert wrong.status_code == 401


@pytest.mark.anyio
async def test_v1_accepts_the_correct_token(app_with_token) -> None:
    transport = ASGITransport(app=app_with_token)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/v1/tools", headers={INTERNAL_TOKEN_HEADER: TOKEN})
        # 令牌正确即被放行：这里断言"不是被门槛拒绝"。MCP 未启动时端点自身会
        # 返回 503（工具清单不可用），那是业务语义，与鉴权无关。
        assert response.status_code != 401
        assert "internal token" not in response.text


@pytest.mark.anyio
async def test_health_stays_open(app_with_token) -> None:
    """健康探测不设门槛：容器 healthcheck 与前端降级提示都要在无凭据时可用。"""
    transport = ASGITransport(app=app_with_token)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/health")).status_code == 200
