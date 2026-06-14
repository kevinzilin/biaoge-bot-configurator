import asyncio
import json
import sys
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
from biaoge_bot.commands import parse_message_text
from biaoge_bot.dispatcher import TriggerContext

def test_parse_commands():
    # 测试帮助
    c = parse_message_text("/help")
    assert c.name == "help", f"Failed: {c}"
    
    # 测试面板
    c = parse_message_text("/panel")
    assert c.name == "panel", f"Failed: {c}"
    
    # 测试运行默认
    c = parse_message_text("/run_default")
    assert c.name == "run_default", f"Failed: {c}"
    
    # 测试重置
    c = parse_message_text("/reset table=klein_table scope=all_nonqueued clear=1")
    assert c.name == "reset", f"Failed: {c}"
    assert c.args == {"table": "klein_table", "scope": "all_nonqueued", "clear": "1"}

    # 测试运行带参数
    c = parse_message_text("/run record=recxxxx seed=1 steps=30 prompt=hello")
    assert c.name == "run", f"Failed: {c}"
    assert c.args == {"record": "recxxxx", "seed": "1", "steps": "30", "prompt": "hello"}
    
    c = parse_message_text("/run row=6 seed=1 steps=30 prompt=hello")
    assert c.args == {"row": "6", "seed": "1", "steps": "30", "prompt": "hello"}

    # 测试 wf 带工作流名
    c = parse_message_text("/wf my_workflow record=recxxxx seed=1 steps=30 prompt=hello")
    assert c.name == "wf", f"Failed: {c}"
    assert c.args == {"workflow": "my_workflow", "record": "recxxxx", "seed": "1", "steps": "30", "prompt": "hello"}
    
    c = parse_message_text("/wf my_workflow row=6 view=vewxxxx")
    assert c.args == {"workflow": "my_workflow", "row": "6", "view": "vewxxxx"}
    
    c = parse_message_text("/wf my_workflow 3.seed=1 10.text=hello")
    assert c.args == {"workflow": "my_workflow", "3.seed": "1", "10.text": "hello"}

    # 测试 batch
    c = parse_message_text("/batch my_workflow table=face_table batch=10 inflight=1")
    assert c.name == "batch", f"Failed: {c}"
    assert c.args == {"workflow": "my_workflow", "table": "face_table", "batch": "10", "inflight": "1"}
    
    # 测试 drain
    c = parse_message_text("/drain my_workflow table=face_table batch=10 inflight=1")
    assert c.name == "drain", f"Failed: {c}"
    assert c.args == {"workflow": "my_workflow", "table": "face_table", "batch": "10", "inflight": "1"}
    
    # 测试 stop_queue
    c = parse_message_text("/stop_queue my_workflow table=face_table")
    assert c.name == "stop_queue", f"Failed: {c}"
    assert c.args == {"workflow": "my_workflow", "table": "face_table"}

    print("All parsing tests passed!")

if __name__ == "__main__":
    test_parse_commands()
