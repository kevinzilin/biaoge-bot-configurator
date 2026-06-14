import asyncio
import httpx
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
from scripts.python_closed_loop_test import _build_context
from biaoge_bot.callback_server import create_callback_app
from biaoge_bot.comfyui import ComfyQueued

async def run_test(client: httpx.AsyncClient, text: str):
    print(f"\n======================================")
    print(f"Testing command: {text}")
    start_time = time.time()
    try:
        res = await asyncio.wait_for(client.post("/_local/exec", json={"text": text}), timeout=60.0)
        data = res.json()
        print(f"Result (took {time.time() - start_time:.2f}s): {data}")
        if not data.get("ok"):
            print(f"[FAIL] Command failed: {data.get('error')}")
        else:
            print(f"[PASS] Command succeeded")
        return data
    except asyncio.TimeoutError:
        print(f"[FAIL] Command timed out after {time.time() - start_time:.2f}s!")
        return {"ok": False, "error": "timeout"}
    except Exception as e:
        print(f"[FAIL] Command raised exception: {e}")
        return {"ok": False, "error": str(e)}

async def reset_data(client: httpx.AsyncClient, table: str = "klein_table"):
    print(f"\n--- Resetting data for {table} ---")
    try:
        await asyncio.wait_for(client.post("/_local/exec", json={"text": f"/reset table={table} scope=all clear=1"}), timeout=30.0)
        print("--- Reset complete ---")
    except Exception as e:
        print(f"--- Reset failed: {e} ---")

async def main():
    ctx = _build_context(env_file=None, workflow_config_path=str(PROJECT_ROOT / "config" / "workflows.loca.json"))
    
    # Mock ComfyUI Client
    ctx.comfyui.queue_workflow = AsyncMock(return_value=ComfyQueued(prompt_id="test_mock_prompt_id", raw={}))
    ctx.comfyui.upload_image = AsyncMock(return_value={"name": "test_image.png", "subfolder": "test", "type": "input"})
    
    # Mock Drive Client to avoid real Feishu downloads which might hang
    if ctx.drive:
        ctx.drive.download_media = AsyncMock(return_value="mocked_downloaded_file.png")
        
    app = create_callback_app(ctx)
    
    table = "klein_table"
    wf_name = "add_real_details"
    
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # 1. 帮助指令
        await run_test(client, "/help")
        
        # 2. 面板指令
        await run_test(client, "/panel")
        
        # 3. 准备数据重置
        await reset_data(client, table)
        
        # 4. 测试 /run_default
        await run_test(client, "/run_default")
        
        # 6. 测试 /run 按行号
        await run_test(client, f"/run row=1 table={table} seed=123")
        
        # 7. 测试 /wf 带工作流名称和行号
        await run_test(client, f"/wf {wf_name} row=2 table={table}")
        
        # 8. 测试 /wf 带有具体节点参数覆盖
        await run_test(client, f"/wf {wf_name} row=3 table={table} 153.seed=999")
        
        # 9. 测试 /batch (队列运行)
        await run_test(client, f"/batch {wf_name} table={table} batch=2 inflight=1")
        
        # 10. 测试 /stop_queue
        await run_test(client, f"/stop_queue {wf_name} table={table}")
        
        # 重置数据以便 drain 测试
        await reset_data(client, table)
        
        # 11. 测试 /drain (最后测试)
        await run_test(client, f"/drain {wf_name} table={table} batch=5 inflight=1")
        
        # 清理
        await client.post("/_local/exec", json={"text": f"/stop_queue {wf_name} table={table}"})

if __name__ == "__main__":
    asyncio.run(main())
