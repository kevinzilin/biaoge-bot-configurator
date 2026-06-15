import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from biaoge_bot.dispatcher import _resolve_relation_item_files
from biaoge_bot.modules.bitable_logic import resolve_relation_param_items
from biaoge_bot.ports import BitableMode
from biaoge_bot.workflows import ParamSpec, ParamTarget, WorkflowSpec


class FakeBitable:
    async def get_record(self, record_id: str) -> dict:
        assert record_id == "rec_related"
        return {
            "record_id": record_id,
            "fields": {
                "副图": [{"file_token": "file_token_1", "name": "relation.png"}],
                "提示词": "来自副表的提示词",
            },
        }


class FakeSearchBitable:
    def __init__(self) -> None:
        self.search_values: list[str] = []
        self.records = {
            "产品标准型主图": {
                "record_id": "rec_main",
                "fields": {"文本": "主图参数"},
            },
            "核心卖点副图": {
                "record_id": "rec_side",
                "fields": {"文本": "副图参数"},
            },
        }

    async def search_records(self, *, filter_: dict | None = None, sort=None, page_size: int = 20) -> list[dict]:
        condition = ((filter_ or {}).get("conditions") or [{}])[0]
        value = ((condition.get("value") or [""])[0] or "")
        self.search_values.append(str(value))
        record = self.records.get(str(value))
        return [{"record_id": record["record_id"]}] if record else []

    async def get_record(self, record_id: str) -> dict:
        for record in self.records.values():
            if record["record_id"] == record_id:
                return record
        raise KeyError(record_id)


class FakeDrive:
    async def download_media(self, *, file_token: str, download_dir: str, file_name: str | None = None, download_url: str | None = None) -> str:
        assert file_token == "file_token_1"
        return str(Path(download_dir) / (file_name or "relation.png"))


class FakeComfyUI:
    async def upload_image(self, *, file_path: str, filename: str, type: str, overwrite: bool, subfolder: str | None) -> dict:
        assert filename == "relation.png"
        return {"name": "uploaded_relation.png", "subfolder": "rel"}


def _ctx(tmp: str) -> SimpleNamespace:
    return SimpleNamespace(
        bitables={"rel_table": FakeBitable()},
        bitable_mode=BitableMode(read_enabled=True, write_enabled=True),
        auth=SimpleNamespace(),
        drive=FakeDrive(),
        settings=SimpleNamespace(
            temp_download_dir=tmp,
            comfyui_upload_enabled=True,
            comfyui_upload_overwrite=True,
            comfyui_upload_subfolder="rel",
            comfyui_input_dir="",
        ),
    )


def _ctx_with_bitable(tmp: str, bitable) -> SimpleNamespace:
    ctx = _ctx(tmp)
    ctx.bitables = {"rel_table": bitable}
    return ctx


def _wf() -> WorkflowSpec:
    return WorkflowSpec(
        key="wf",
        workflow_name="wf",
        api_workflow_path=None,
        params={
            "images": ParamSpec(targets=(ParamTarget(node_id="1", field_name="image", index=0),), type="str", multi=True),
            "prompt": ParamSpec(targets=(ParamTarget(node_id="2", field_name="text"),), type="str", multi=False),
        },
    )


async def _run() -> None:
    with tempfile.TemporaryDirectory() as td:
        ctx = _ctx(td)
        items = await resolve_relation_param_items(
            ctx,
            source_value={"record_id": "rec_related"},
            target_app_token=None,
            target_table_id=None,
            target_table_key="rel_table",
            target_match_field=None,
            item_param_map={"images": "副图", "prompt": "提示词"},
            prompt_fields=None,
            join_with="\n",
            prompt_param=None,
            max_items=20,
            strict=True,
        )
        assert len(items) == 1
        assert items[0]["prompt"] == "来自副表的提示词"
        assert isinstance(items[0]["images"], list)
        assert items[0]["images"][0]["file_token"] == "file_token_1"

        resolved = await _resolve_relation_item_files(
            ctx,
            provider="comfyui",
            comfyui=FakeComfyUI(),
            runninghub=None,
            wf=_wf(),
            items=items,
            resolve_files=True,
        )
        assert resolved[0]["prompt"] == "来自副表的提示词"
        assert resolved[0]["images"] == ["rel/uploaded_relation.png"]

        search_bitable = FakeSearchBitable()
        items2 = await resolve_relation_param_items(
            _ctx_with_bitable(td, search_bitable),
            source_value="产品标准型主图,核心卖点副图",
            target_app_token=None,
            target_table_id=None,
            target_table_key="rel_table",
            target_match_field="副表2",
            item_param_map={"test_txt": "文本"},
            prompt_fields=None,
            join_with="\n",
            prompt_param=None,
            max_items=20,
            strict=True,
        )
        assert search_bitable.search_values == ["产品标准型主图", "核心卖点副图"]
        assert [item["test_txt"] for item in items2] == ["主图参数", "副图参数"]


def main() -> None:
    asyncio.run(_run())
    print("Relation attachment param tests passed!")


if __name__ == "__main__":
    main()
