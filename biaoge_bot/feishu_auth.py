from __future__ import annotations

import time

import httpx


class FeishuAuth:
    def __init__(self, app_id: str, app_secret: str) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._token: str | None = None
        self._expire_at: float = 0.0

    async def tenant_token(self) -> str:
        now = time.time()
        if self._token and now + 30 < self._expire_at:
            return self._token

        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self._app_id, "app_secret": self._app_secret},
            )
            r.raise_for_status()
            data = r.json()
            token = data.get("tenant_access_token")
            expire = data.get("expire", 0)
            if not token:
                raise RuntimeError(f"feishu auth failed: {data}")
            self._token = token
            self._expire_at = now + int(expire or 0)
            return token

