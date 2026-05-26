from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from .feishu_auth import FeishuAuth


def _guess_mime(path: str) -> str:
    low = str(path or "").lower()
    if low.endswith(".png"):
        return "image/png"
    if low.endswith(".jpg") or low.endswith(".jpeg"):
        return "image/jpeg"
    if low.endswith(".webp"):
        return "image/webp"
    if low.endswith(".gif"):
        return "image/gif"
    if low.endswith(".bmp"):
        return "image/bmp"
    if low.endswith(".mp4"):
        return "video/mp4"
    if low.endswith(".mp3"):
        return "audio/mpeg"
    if low.endswith(".wav"):
        return "audio/wav"
    if low.endswith(".opus"):
        return "audio/opus"
    return "application/octet-stream"


def _guess_file_type(path: str) -> str:
    ext = Path(path).suffix.lower().lstrip(".")
    return ext if ext else "bin"


class IMClient:
    def __init__(self, auth: FeishuAuth) -> None:
        self._auth = auth

    async def send_text(self, *, chat_id: str, text: str) -> None:
        token = await self._auth.tenant_token()
        url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
        payload = {
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, headers={"Authorization": f"Bearer {token}"}, json=payload)
            r.raise_for_status()
            data = r.json()
            if data.get("code") not in (0, None):
                raise RuntimeError(f"send_text failed: {data}")

    async def send_interactive_card(self, *, chat_id: str, card: dict[str, Any]) -> None:
        token = await self._auth.tenant_token()
        url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
        payload = {
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        }
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, headers={"Authorization": f"Bearer {token}"}, json=payload)
            r.raise_for_status()
            data = r.json()
            if data.get("code") not in (0, None):
                raise RuntimeError(f"send_interactive_card failed: {data}")

    async def upload_image_message(self, *, file_path: str) -> str:
        token = await self._auth.tenant_token()
        url = "https://open.feishu.cn/open-apis/im/v1/images"
        name = Path(file_path).name
        async with httpx.AsyncClient(timeout=30) as client:
            with open(file_path, "rb") as f:
                r = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    data={"image_type": "message"},
                    files={"image": (name, f, _guess_mime(file_path))},
                )
            r.raise_for_status()
            data = r.json()
            if data.get("code") not in (0, None):
                raise RuntimeError(f"upload_image_message failed: {data}")
            key = (data.get("data") or {}).get("image_key")
            if not isinstance(key, str) or not key:
                raise RuntimeError(f"upload_image_message missing image_key: {data}")
            return key

    async def send_image(self, *, chat_id: str, image_key: str) -> None:
        token = await self._auth.tenant_token()
        url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
        payload = {
            "receive_id": chat_id,
            "msg_type": "image",
            "content": json.dumps({"image_key": image_key}, ensure_ascii=False),
        }
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, headers={"Authorization": f"Bearer {token}"}, json=payload)
            r.raise_for_status()
            data = r.json()
            if data.get("code") not in (0, None):
                raise RuntimeError(f"send_image failed: {data}")

    async def upload_file_message(self, *, file_path: str) -> str:
        token = await self._auth.tenant_token()
        url = "https://open.feishu.cn/open-apis/im/v1/files"
        name = Path(file_path).name
        file_type = _guess_file_type(file_path)
        async with httpx.AsyncClient(timeout=60) as client:
            with open(file_path, "rb") as f:
                r = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    data={"file_type": file_type, "file_name": name},
                    files={"file": (name, f, _guess_mime(file_path))},
                )
            r.raise_for_status()
            data = r.json()
            if data.get("code") not in (0, None):
                raise RuntimeError(f"upload_file_message failed: {data}")
            key = (data.get("data") or {}).get("file_key")
            if not isinstance(key, str) or not key:
                raise RuntimeError(f"upload_file_message missing file_key: {data}")
            return key

    async def send_file(self, *, chat_id: str, file_key: str) -> None:
        token = await self._auth.tenant_token()
        url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
        payload = {
            "receive_id": chat_id,
            "msg_type": "file",
            "content": json.dumps({"file_key": file_key}, ensure_ascii=False),
        }
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, headers={"Authorization": f"Bearer {token}"}, json=payload)
            r.raise_for_status()
            data = r.json()
            if data.get("code") not in (0, None):
                raise RuntimeError(f"send_file failed: {data}")
