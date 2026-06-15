from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class BitableMode:
    read_enabled: bool
    write_enabled: bool


_OFF_MODE_NAMES = {"off", "none", "disable", "disabled"}
_READ_MODE_NAMES = {"read", "readonly", "ro"}
_WRITE_MODE_NAMES = {"write", "writeonly", "wo"}
_READWRITE_MODE_NAMES = {"readwrite", "rw", "all", "on", "enable", "enabled"}


def normalize_bitable_mode_name(mode: Any) -> str:
    raw = str(mode or "").strip().lower()
    if raw in _OFF_MODE_NAMES:
        return "off"
    if raw in _READ_MODE_NAMES:
        return "read"
    if raw in _WRITE_MODE_NAMES:
        return "write"
    if raw in _READWRITE_MODE_NAMES:
        return "readwrite"
    if raw == "auto":
        return "auto"
    return "readwrite"


def bitable_clients_enabled(mode: BitableMode | None) -> bool:
    return bool(mode and (mode.read_enabled or mode.write_enabled))


def bitable_write_enabled(mode: BitableMode | None) -> bool:
    return bool(mode and mode.write_enabled)


def bitable_input_read_enabled(settings_mode: Any, mode: BitableMode | None) -> bool:
    """True when record field values may be used as workflow input parameters."""

    if normalize_bitable_mode_name(settings_mode) in ("off", "write"):
        return False
    return bool(mode and mode.read_enabled)


def bitable_event_enabled(settings_mode: Any, mode: BitableMode | None) -> bool:
    """True when Bitable change events may trigger workflow execution."""

    return bitable_input_read_enabled(settings_mode, mode)


def ctx_bitable_clients_enabled(ctx: Any) -> bool:
    return bitable_clients_enabled(getattr(ctx, "bitable_mode", None))


def ctx_bitable_write_enabled(ctx: Any) -> bool:
    return bitable_write_enabled(getattr(ctx, "bitable_mode", None))


def ctx_bitable_input_read_enabled(ctx: Any) -> bool:
    settings = getattr(ctx, "settings", None)
    return bitable_input_read_enabled(getattr(settings, "bitable_mode", ""), getattr(ctx, "bitable_mode", None))


def ctx_bitable_event_enabled(ctx: Any) -> bool:
    settings = getattr(ctx, "settings", None)
    return bitable_event_enabled(getattr(settings, "bitable_mode", ""), getattr(ctx, "bitable_mode", None))


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

    async def create_field(self, *, field_name: str, field_type: int) -> dict[str, Any]: ...


class DrivePort(Protocol):
    async def download_media(self, *, file_token: str, download_dir: str, file_name: str | None = None) -> str: ...

    async def upload_to_bitable(self, *, app_token: str, file_path: str, as_image: bool) -> Any: ...
