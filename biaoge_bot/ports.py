from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class BitableMode:
    read_enabled: bool
    write_enabled: bool


@dataclass(frozen=True)
class BitableConfig:
    app_token: str
    table_id: str
    view_id: str | None
    fields: dict[str, str]
    status_values: dict[str, str]


class BitablePort(Protocol):
    @property
    def config(self) -> BitableConfig: ...

    @property
    def mode(self) -> BitableMode: ...

    async def get_record(self, record_id: str) -> dict[str, Any]: ...

    async def update_record(self, record_id: str, fields: dict[str, Any]) -> None: ...

    async def search_records(
        self,
        *,
        filter_: dict[str, Any] | None = None,
        sort: list[dict[str, Any]] | None = None,
        page_size: int = 20,
    ) -> list[dict[str, Any]]: ...

    async def search_records_page(
        self,
        *,
        filter_: dict[str, Any] | None = None,
        sort: list[dict[str, Any]] | None = None,
        page_size: int = 20,
        page_token: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None, bool]: ...

    async def list_records_page(
        self,
        *,
        view_id: str | None = None,
        page_size: int = 20,
        page_token: str | None = None,
        automatic_fields: bool | None = None,
    ) -> tuple[list[dict[str, Any]], str | None, bool, int]: ...

    async def list_fields_page(
        self,
        *,
        page_size: int = 200,
        page_token: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None, bool]: ...

    async def list_fields(self) -> list[dict[str, Any]]: ...

    async def find_next_queued_record_id(self) -> str | None: ...


class DrivePort(Protocol):
    async def download_media(self, *, file_token: str, download_dir: str, file_name: str | None = None) -> str: ...

    async def upload_to_bitable(self, *, app_token: str, file_path: str, as_image: bool) -> Any: ...
