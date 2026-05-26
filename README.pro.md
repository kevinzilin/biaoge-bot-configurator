# Biaoge（高级版说明）

本文档对应“高级版”（包含飞书多维表格/Drive 回写能力，以及更完整的闭环与排查说明）。

如果你只需要 BITABLE_MODE=off 的开源版本，请阅读仓库根目录的 README.md。

## 能力概览

- 飞书 Socket Mode 常驻：接收群消息指令与卡片回调事件
- ComfyUI 触发：WorkflowPrompt（/prompt_workflow）+ nodeInfoList 参数替换 + extra_data 透传
- 回调处理：
  - 本地 ComfyUI：Webhook-Callback 插件直打本机 callback_server
  - 私有云 ComfyUI / RunningHub：通过公网转发器（阿里云 FC）中转，群内 @ 触发本地执行
- 多维表格与 Drive（可选）：
  - 表格队列（queued/running/done/failed）流转
  - 输入附件下载、输出附件上传与写回

## 指令

参数写法（很重要）：

- 统一用“空格分隔”的 `key=value`（例如 `seed=1 steps=30`）
- 如果值里有空格，建议不要用这种值（会被拆成多段，机器人读不懂）；优先改成不含空格的值，或改用工作流 key

基础指令：

- `/help`：查看帮助  
  示例：`/help`
- `/panel`：发送控制面板卡片  
  示例：`/panel`
- `/ids`：回显 chat_id 与 user_open_id  
  示例：`/ids`
- `/botid`：回显 bot_open_id（用于配置转发器的 FEISHU_AT_USER_ID）  
  示例：`/botid`

运行工作流：

- `/run_default`：运行默认工作流（从 workflows 配置里读取 default_workflow；通常用于“跑队列”场景）  
  示例：`/run_default`
- `/run ...`：运行“默认工作流”，并支持指定表格记录/行号 + 覆盖参数  
  示例：`/run record=recxxxx seed=1 steps=30 prompt=hello`  
  示例：`/run row=6 seed=1 steps=30 prompt=hello`
- `/wf <workflowKey> ...`：运行指定 workflow key，并支持指定表格记录/行号 + 覆盖参数  
  示例：`/wf klein_add_real_details record=recxxxx seed=1 steps=30 prompt=hello`  
  示例：`/wf klein_add_real_details row=6 view=vewxxxx`  
  示例：`/wf klein_add_real_details 3.seed=1 10.text=hello`

队列/跑批（需要表格配置与授权可用）：

- `/batch <workflowKey> table=<tableKey> batch=<N> inflight=<N>`：从表格里取 queued 任务，批量跑一段  
  示例：`/batch klein_add_real_details table=klein_table batch=10 inflight=1`
- `/drain <workflowKey> table=<tableKey> batch=<N> inflight=<N>`：持续处理队列直到耗尽  
  示例：`/drain klein_add_real_details table=klein_table batch=10 inflight=1`
- `/stop_queue <workflowKey> table=<tableKey>`：停止当前的批量/队列任务  
  示例：`/stop_queue klein_add_real_details table=klein_table`

重置（用于卡死/异常后恢复；需要表格可读）：

- `/reset table=<tableKey> scope=<scope> clear=<0|1>`：重置某张表的运行态  
  示例：`/reset table=klein_table scope=all_nonqueued clear=0`
- `/reset_table table=<tableKey> scope=<scope> clear=<0|1>`：同上（名字不同，效果一致）  
  示例：`/reset_table table=klein_table scope=all clear=1`

回调触发（公网转发器用，正常不需要手动调用）：

- `/cb provider=<comfyui|runninghub> id=<taskId> sig=<token>`  
  示例：`/cb provider=runninghub id=2058921669074444290 sig=xxxx`
- `/cb data=<base64url_json> sig=<token>`（把完整 payload 编码进 data）  
  示例：`/cb data=eyJwcm92aWRlciI6InJ1bm5pbmdo... sig=xxxx`

群聊里如果你希望“@ 机器人 + 指令”也能生效，写法可以是：

- `@机器人 /ids`
- `/ids @机器人`

## 公网转发器（阿里云 FC）配置要点

转发器示例代码：`aliyun_fc_forwarder/handler.py`

- 回调 URL 建议携带 `?token=...`，FC 用 `WEBHOOK_TOKEN` 校验
- FC 会往群里发送 `/cb provider=... id=... sig=...` 并 @ 本地机器人触发处理

FC 环境变量（最低）：

- `FEISHU_APP_ID` / `FEISHU_APP_SECRET`
- `FEISHU_RECEIVE_ID`：群 chat_id
- `FEISHU_AT_USER_ID`：本地机器人的 bot_open_id
- `WEBHOOK_TOKEN`（可选，但推荐）
- `CB_MESSAGE_TOKEN`（可选，但推荐）

本地程序常用指令：

- `/ids`：回显 chat_id 与 user_open_id
- `/botid`：回显 bot_open_id（用于配置转发器的 FEISHU_AT_USER_ID）
- `/cb`：公网转发器触发用（正常不需要手动调用）
