from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .comfyui import ComfyUIClient
from .config import Settings
from .feishu_auth import FeishuAuth
from .ports import BitableConfig, BitableMode, BitablePort, DrivePort
from .workflows import WorkflowRegistry


@dataclass(frozen=True)
class AppContext:
    settings: Settings
    config: dict[str, Any]
    auth: FeishuAuth
    bitables: dict[str, BitablePort]
    drive: DrivePort | None
    comfyui: ComfyUIClient
    workflows: WorkflowRegistry
    bitable_mode: BitableMode
    bitable_configs: dict[str, BitableConfig]
    default_table_key: str | None
    default_workflow_key: str | None
    runner: Any
