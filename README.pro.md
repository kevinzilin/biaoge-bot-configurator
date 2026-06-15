# Biaoge（高级版说明）

本文档对应“高级版”（包含飞书多维表格/Drive 回写能力，以及更完整的闭环与排查说明）。

如果你只需要 BITABLE_MODE=off 的开源版本，请阅读仓库根目录的 README.md。

受保护模块的源码、编译依赖和 Windows/macOS/Linux 编译步骤见 [docs/protected_modules_build.md](docs/protected_modules_build.md)。

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
- 如果值里有空格，用引号包起来（现在支持）：例如 `prompt="hello world"`、`images="@./pics/my a.jpg"`
- 如果你想让机器人“先上传本机文件再执行”，在值前面加 `@`：例如 `images=@./pics/a.jpg`

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
- `/run ...`：运行“默认工作流”，并支持指定表格记录/行号 + 覆盖参数；也支持显式指定 `workflow=... table=...`  
  示例：`/run record=recxxxx seed=1 steps=30 prompt=hello`  
  示例：`/run row=6 seed=1 steps=30 prompt=hello`  
  示例：`/run workflow=klein_add_real_details table=klein_table prompt=hello`
- `/wf <workflowKey> ...`：运行指定 workflow key，并支持指定表格记录/行号 + 覆盖参数；如果命令未指定 table，且该 workflow 也未绑定 table，则按“纯参数直跑”处理  
  示例：`/wf klein_add_real_details record=recxxxx seed=1 steps=30 prompt=hello`  
  示例：`/wf klein_add_real_details row=6 view=vewxxxx`  
  示例：`/wf klein_add_real_details 3.seed=1 10.text=hello`  
  示例：`/wf klein_add_real_details images="@./pics/my a.jpg" prompt="hello world"`

队列/跑批（需要表格配置与授权可用）：

- `/batch <workflowKey> table=<tableKey> batch=<N> inflight=<N>`：从表格里取 queued 任务，批量跑一段（table 可省略：优先用该 workflow 配置的 table；未配置则用 default_table）  
  示例：`/batch klein_add_real_details table=klein_table batch=10 inflight=1`  
  示例：`/batch klein_add_real_details batch=10 inflight=1`
- `/drain <workflowKey> table=<tableKey> batch=<N> inflight=<N>`：持续处理队列直到耗尽（table 可省略：优先用该 workflow 配置的 table；未配置则用 default_table）  
  示例：`/drain klein_add_real_details table=klein_table batch=10 inflight=1`  
  示例：`/drain klein_add_real_details batch=10 inflight=1`
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

## BITABLE_MODE 情景（只读/只写/读写/关闭）

补充说明（很重要）：

- `BITABLE_MODE=auto` 的意思是“自动启用表格能力”，不是“所有命令都自动去读 default_table”
- 在 `auto` 下，命令是否读表，取决于“这条命令需不需要表”以及“是否显式指定了 table / workflow 是否绑定了 table”
- 当前命令选表规则：
  - `/wf <workflow> ...`：`table 参数 > workflow.table > 纯参数直跑`
  - `/run workflow=... ...`：`table 参数 > workflow.table > 纯参数直跑`
  - 裸 `/run ...`：按默认入口处理，可使用 `default_workflow / default_table`
  - `/batch /drain /stop_queue`：`table 参数 > workflow.table > default_table`

### 情景1：关闭表格读写（BITABLE_MODE=off）

- 用法：只靠指令传参入队，不读表、不写表
- 示例（直接指定参数）：
  - `/wf klein_add_real_details prompt="hello world"`
- 示例（带附件：先上传本机文件再执行）：
  - `/wf klein_add_real_details images="@./pics/my a.jpg" prompt="hello world"`
- 注意：
  - 因为不读表格，不会自动下载表格附件；你需要通过指令直传参数
  - 附件输入推荐用 `@本机路径`：机器人会先上传（ComfyUI/RunningHub）再替换为执行端识别的文件名
  - 私有云 ComfyUI / RunningHub：若要接收回调并发消息，需要配置公网转发器（REMOTE_CALLBACK_URL）

### 情景2：只开启表格读（BITABLE_MODE=read）

- 用法：从表格读取字段/附件，入队时替换参数，但不回写状态/结果
- 示例（指定记录）：
  - `/wf klein_add_real_details record=recxxxx table=prod_table`
- 示例（用行号定位记录）：
  - `/wf klein_add_real_details row=6 table=prod_table`
  - 如果你想按“某个视图里看到的第 N 行”对齐，临时指定视图：`/wf klein_add_real_details row=6 view=vewxxxx table=prod_table`
- 注意：
  - 只读模式不会写回，所以表格里状态不会变化
  - 如果你执行的是 `/wf <workflow> ...` 或 `/run workflow=... ...`，但既没有传 `table=...`，该 workflow 也没有绑定 `table`，那么这条命令会按“纯参数直跑”处理，不会自动去读默认表
  - 表格附件会先下载到本机，再根据执行端做处理：
    - RunningHub：会自动上传，传入参数会替换成 `openapi/...` 这种文件名
    - ComfyUI：如果 `COMFYUI_UPLOAD_ENABLED=1` 会自动上传到 ComfyUI input；否则会尝试用 `COMFYUI_INPUT_DIR`/本地路径（取决于你的配置与节点兼容性）

### 情景3：开启表格读写（BITABLE_MODE=readwrite）

- 用法：本项目主模式（队列跑批 + 回写状态/结果）
- 示例：
  - `/drain klein_add_real_details table=prod_table batch=10 inflight=1`

### 情景4：只开启表格写（BITABLE_MODE=write）

- 用法：参数从指令传入；输出写回表格；不从表格读取输入字段/附件
- 示例（写回指定记录）：
  - `/wf klein_add_real_details record=recxxxx table=prod_table prompt="hello"`
- 示例（写回第 N 行对应的记录，支持 row=4）：
  - `/wf klein_add_real_details row=4 table=prod_table prompt="hello"`
- 注意：
  - 只写模式下，即使表格里有“提示词/参考图”等字段，也不会被当作输入读取；你仍需要在指令里把参数传全（包括附件用 `@...`）
  - `row=...` 只用于定位要写回的记录，不会读取该行字段作为任务参数
  - 只写模式不会订阅或处理多维表格事件触发
  - 如果你执行的是 `/wf <workflow> ...` 或 `/run workflow=... ...`，但既没有传 `table=...`，该 workflow 也没有绑定 `table`，那么这条命令会按“纯参数直跑”处理，不会自动去碰默认表
  - 如果没有提供 `record/row`，程序不会从表格里自动取下一条 queued 记录作为目标

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

结果交付开关：

- `FEISHU_SEND_RESULT_TO_CHAT=0`：保持旧行为，仅无表格/无记录的直跑任务会把结果发回对话框
- `FEISHU_SEND_RESULT_TO_CHAT=1`：绑定表格并回写时，也同步把生成结果发回触发的飞书对话框
