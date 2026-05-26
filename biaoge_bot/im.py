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


def _raise_http(op: str, r: httpx.Response) -> None:
    ct = str(r.headers.get("content-type") or "").lower()
    if "application/json" in ct:
        try:
            data = r.json()
        except Exception:
            data = r.text
        raise RuntimeError(f"{op} failed ({r.status_code}): {data}")
    raise RuntimeError(f"{op} failed ({r.status_code}): {r.text}")


class IMClient:
    def __init__(self, auth: FeishuAuth) -> None:
        self._auth = auth

    async def _send_message(self, *, chat_id: str, msg_type: str, content: dict[str, Any] | None = None) -> None:
        token = await self._auth.tenant_token()
        url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
        payload: dict[str, Any] = {"receive_id": chat_id, "msg_type": str(msg_type or "").strip()}
        if payload["msg_type"] in ("text", "interactive"):
            payload["content"] = json.dumps(content or {}, ensure_ascii=False)
        else:
            payload["content"] = json.dumps(content or {}, ensure_ascii=False)
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, headers={"Authorization": f"Bearer {token}"}, json=payload)
        if r.status_code >= 400:
            _raise_http("send_message", r)
        data = r.json()
        if data.get("code") not in (0, None):
            raise RuntimeError(f"send_message failed: {data}")

    async def _upload_im_file(
        self,
        *,
        file_path: str,
        file_type: str,
        file_name: str | None = None,
        duration_ms: int | None = None,
    ) -> str:
        token = await self._auth.tenant_token()
        url = "https://open.feishu.cn/open-apis/im/v1/files"
        name = file_name or Path(file_path).name
        ft = str(file_type or "").strip() or "stream"
        data: dict[str, str] = {"file_type": ft, "file_name": name}
        if duration_ms is not None:
            data["duration"] = str(int(duration_ms))
        async with httpx.AsyncClient(timeout=60) as client:
            with open(file_path, "rb") as f:
                r = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    data=data,
                    files={"file": (name, f, _guess_mime(file_path))},
                )
            if r.status_code >= 400:
                _raise_http("upload_im_file", r)
            data1 = r.json()
            if data1.get("code") not in (0, None):
                raise RuntimeError(f"upload_im_file failed: {data1}")
            key = (data1.get("data") or {}).get("file_key")
            if not isinstance(key, str) or not key:
                raise RuntimeError(f"upload_im_file missing file_key: {data1}")
            return key

    async def download_message_resource(
        self,
        *,
        message_id: str,
        file_key: str,
        resource_type: str,
        save_path: str,
    ) -> None:
        token = await self._auth.tenant_token()
        mid = str(message_id or "").strip()
        fkey = str(file_key or "").strip()
        rtype = str(resource_type or "").strip()
        if not mid:
            raise RuntimeError("missing message_id")
        if not fkey:
            raise RuntimeError("missing file_key")
        if not rtype:
            raise RuntimeError("missing resource_type")
        url = f"https://open.feishu.cn/open-apis/im/v1/messages/{mid}/resources/{fkey}"
        p = Path(save_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            r = await client.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                params={"type": rtype},
            )
            if r.status_code >= 400:
                _raise_http("download_message_resource", r)
            ct = str(r.headers.get("content-type") or "").lower()
            if "application/json" in ct:
                data = r.json()
                raise RuntimeError(f"download_message_resource failed: {data}")
            p.write_bytes(r.content)

    async def send_text(self, *, chat_id: str, text: str) -> None:
        await self._send_message(chat_id=chat_id, msg_type="text", content={"text": text})

    async def send_interactive_card(self, *, chat_id: str, card: dict[str, Any]) -> None:
        await self._send_message(chat_id=chat_id, msg_type="interactive", content=card)

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
            if r.status_code >= 400:
                _raise_http("upload_image_message", r)
            data = r.json()
            if data.get("code") not in (0, None):
                raise RuntimeError(f"upload_image_message failed: {data}")
            key = (data.get("data") or {}).get("image_key")
            if not isinstance(key, str) or not key:
                raise RuntimeError(f"upload_image_message missing image_key: {data}")
            return key

    async def send_image(self, *, chat_id: str, image_key: str) -> None:
        await self._send_message(chat_id=chat_id, msg_type="image", content={"image_key": image_key})

    async def upload_file_message(self, *, file_path: str) -> str:
        file_type = _guess_file_type(file_path)
        try:
            return await self._upload_im_file(file_path=file_path, file_type=file_type)
        except Exception:
            return await self._upload_im_file(file_path=file_path, file_type="stream")

    async def upload_audio_message(self, *, file_path: str, duration_ms: int | None = None) -> str:
        file_type = _guess_file_type(file_path)
        return await self._upload_im_file(file_path=file_path, file_type=file_type, duration_ms=duration_ms)

    async def upload_video_message(self, *, file_path: str, duration_ms: int | None = None) -> str:
        file_type = _guess_file_type(file_path)
        return await self._upload_im_file(file_path=file_path, file_type=file_type, duration_ms=duration_ms)

    async def send_file(self, *, chat_id: str, file_key: str) -> None:
        await self._send_message(chat_id=chat_id, msg_type="file", content={"file_key": file_key})

    async def send_audio(self, *, chat_id: str, file_key: str) -> None:
        await self._send_message(chat_id=chat_id, msg_type="audio", content={"file_key": file_key})

    async def send_media(self, *, chat_id: str, file_key: str, cover_image_key: str | None = None) -> None:
        content: dict[str, Any] = {"file_key": file_key}
        if isinstance(cover_image_key, str) and cover_image_key.strip():
            content["image_key"] = cover_image_key.strip()
        await self._send_message(chat_id=chat_id, msg_type="media", content=content)

    async def download_image(self, *, image_key: str, save_path: str, message_id: str | None = None) -> None:
        key = str(image_key or "").strip()
        if not key:
            raise RuntimeError("missing image_key")
        mid = str(message_id or "").strip()
        if mid:
            try:
                await self.download_message_resource(
                    message_id=mid,
                    file_key=key,
                    resource_type="image",
                    save_path=save_path,
                )
                return
            except Exception:
                pass

        token = await self._auth.tenant_token()
        url = f"https://open.feishu.cn/open-apis/im/v1/images/{key}"
        p = Path(save_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            last: httpx.Response | None = None
            for t in ("message", ""):
                params: dict[str, str] | None = None
                if t:
                    params = {"type": t}
                r = await client.get(url, headers={"Authorization": f"Bearer {token}"}, params=params)
                last = r
                if 200 <= r.status_code < 300:
                    ct = str(r.headers.get("content-type") or "").lower()
                    if "application/json" in ct:
                        data = r.json()
                        raise RuntimeError(f"download_image failed: {data}")
                    p.write_bytes(r.content)
                    return
                if r.status_code not in (400, 404):
                    break
            if last is None:
                raise RuntimeError("download_image failed: no response")
            try:
                data = last.json()
                raise RuntimeError(f"download_image failed ({last.status_code}): {data}")
            except Exception:
                raise RuntimeError(f"download_image failed ({last.status_code}): {last.text}")

    async def download_file(
        self,
        *,
        file_key: str,
        save_path: str,
        message_id: str | None = None,
        resource_type: str | None = None,
    ) -> None:
        key = str(file_key or "").strip()
        if not key:
            raise RuntimeError("missing file_key")
        mid = str(message_id or "").strip()
        if mid:
            try:
                await self.download_message_resource(
                    message_id=mid,
                    file_key=key,
                    resource_type=str(resource_type or "file").strip() or "file",
                    save_path=save_path,
                )
                return
            except Exception:
                pass

        token = await self._auth.tenant_token()
        url = f"https://open.feishu.cn/open-apis/im/v1/files/{key}"
        p = Path(save_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            r = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            if r.status_code >= 400:
                _raise_http("download_file", r)
            ct = str(r.headers.get("content-type") or "").lower()
            if "application/json" in ct:
                data = r.json()
                raise RuntimeError(f"download_file failed: {data}")
            p.write_bytes(r.content)

    async def list_chat_messages(self, *, chat_id: str, page_size: int = 20) -> list[dict[str, Any]]:
        token = await self._auth.tenant_token()
        cid = str(chat_id or "").strip()
        if not cid:
            return []
        size = int(page_size or 20)
        size = max(1, min(50, size))
        url = "https://open.feishu.cn/open-apis/im/v1/messages"
        last_err: Exception | None = None
        async with httpx.AsyncClient(timeout=10) as client:
            for t in ("chat", "chat_id"):
                try:
                    params = {"container_id_type": t, "container_id": cid, "page_size": size, "sort_type": "ByCreateTimeDesc"}
                    r = await client.get(url, headers={"Authorization": f"Bearer {token}"}, params=params)
                    if r.status_code >= 400:
                        _raise_http("list_chat_messages", r)
                    data = r.json()
                    if data.get("code") not in (0, None):
                        raise RuntimeError(f"list_chat_messages failed: {data}")
                    items = (data.get("data") or {}).get("items") or []
                    if isinstance(items, list):
                        return [x for x in items if isinstance(x, dict)]
                    return []
                except Exception as e:
                    last_err = e
                    continue
        if last_err:
            raise last_err
        return []
