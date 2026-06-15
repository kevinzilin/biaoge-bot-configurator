import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from biaoge_bot.runninghub import RunningHubClient


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeAsyncClient:
    payloads = []
    requests = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, *, content=None, headers=None, files=None):
        self.requests.append({"url": url, "content": content, "headers": headers, "files": files})
        return FakeResponse(self.payloads.pop(0))


async def _no_sleep(seconds):
    return None


async def _run_task_queue_maxed_retry_test():
    FakeAsyncClient.payloads = [
        {"code": 421, "msg": "TASK_QUEUE_MAXED"},
        {"code": 0, "data": {"taskId": "rh-task-1"}},
    ]
    FakeAsyncClient.requests = []

    with patch("biaoge_bot.runninghub.httpx.AsyncClient", FakeAsyncClient), patch("biaoge_bot.runninghub.asyncio.sleep", _no_sleep):
        client = RunningHubClient(api_key="test-key", base_url="https://example.test")
        created = await client.create_task(
            workflow_id="wf-1",
            node_info_list=[{"nodeId": "1", "fieldName": "text", "fieldValue": "hello"}],
            task_queue_maxed_retries=1,
            task_queue_maxed_retry_delay_seconds=0,
        )

    assert created.task_id == "rh-task-1"
    assert len(FakeAsyncClient.requests) == 2


def test_task_queue_maxed_is_retried():
    asyncio.run(_run_task_queue_maxed_retry_test())


if __name__ == "__main__":
    test_task_queue_maxed_is_retried()
    print("RunningHub retry tests passed!")
