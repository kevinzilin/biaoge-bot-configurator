# Biaoge Bot（开源版）

通过飞书 Socket Mode 远程触发 ComfyUI / RunningHub 任务，适合“脚本在内网运行，不做内网穿透”的场景。

本仓库发布的是 **BITABLE_MODE=off 的开源版**：不包含飞书多维表格与 Drive 回写能力，仅提供“触发 + 回调 + 消息交付”的闭环。

## 功能

- 飞书 Socket Mode：群内指令触发工作流
- ComfyUI（本地/私有云）：WorkflowPrompt（/prompt_workflow）提交任务
- RunningHub：创建任务、上传输入文件、查询结果
- 结果交付：回调完成后在群里发结果（图片/文件/URL）
- 公网中转：可选阿里云 FC 转发器，用于私有云/RunningHub 回调打到内网

## 快速开始

1) 安装依赖

```bash
# Windows
install.cmd
# 或
powershell -ExecutionPolicy Bypass -File win_install.ps1

# macOS / Linux
bash install.sh
```

2) 配置 `.env`

从 `.env.example` 复制一份：

```bash
copy .env.example .env
```

最小必填：

- `FEISHU_APP_ID` / `FEISHU_APP_SECRET`
- `BITABLE_MODE=off`
- `COMFYUI_BASE_URL`（本地或私有云）

RunningHub（可选）：

- `RUNNINGHUB_API_KEY`

私有云 ComfyUI / RunningHub 回调（可选，建议用 FC 中转）：

- `REMOTE_CALLBACK_URL`
- `CB_MESSAGE_TOKEN`

3) 启动

```bash
# Windows
start.cmd
# 或
powershell -ExecutionPolicy Bypass -File win_start.ps1

# macOS / Linux
bash start.sh
```

## 跨平台路径与常驻运行

路径配置支持相对项目根目录、绝对路径、`~` 和 `${BIAOGE_ROOT}`。推荐在 `.env` 和 `config/workflows.local.json` 中使用正斜杠 `/`，例如：

```env
WORKFLOW_CONFIG_PATH=config/workflows.local.json
RESULT_OUTPUT_DIR=output
CALLBACK_DUMP_ENABLED=0
SAVE_TASK_REQUEST_PARAMS=0
```

所有平台脚本统一为“薄壳脚本 + Python 核心”：

```bash
scripts/bootstrap.py  # 安装：创建 .env、.venv、安装 requirements.txt
scripts/preflight.py  # 预检：修复路径、检查端口、补齐必要配置
scripts/launch.py     # 启动：先预检，再进入 supervisor
```

手动启动脚本都会调用 `scripts/launch.py --interactive`，缺少 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` 时会提示输入，缺少 `ADMIN_TOKEN` 时会自动生成。

`scripts/supervisor.py` 是唯一守护入口，负责异常重启、日志、防重复启动、防睡眠；通常不需要手动直接调用。

启用开机启动会注册 `scripts/launch.py --non-interactive`。非交互模式下如果关键配置缺失，会写出错误并退出，避免开机任务卡在输入提示。

```bash
# Windows
powershell -ExecutionPolicy Bypass -File scripts/enable_autostart.ps1 -Mode Logon

# macOS
bash scripts/enable_autostart_macos.sh

# Linux
bash scripts/enable_autostart_linux.sh
```

启用自启脚本只注册开机启动，不安装依赖，也不会默认立刻启动当前进程。需要注册后立即启动时：

```bash
# Windows
powershell -ExecutionPolicy Bypass -File scripts/enable_autostart.ps1 -Mode Logon -RunNow

# macOS
bash scripts/enable_autostart_macos.sh --now

# Linux
bash scripts/enable_autostart_linux.sh --now
```

运行日志统一写入：

```text
logs/biaoge_bot-YYYY-MM-DD.log
```

手动启动时，日志会同时显示在当前命令行窗口和当天日志文件。开机启动/后台启动时，主要查看当天日志文件，例如 `logs/biaoge_bot-2026-06-14.log`。

macOS 的 LaunchAgent 还会额外写入 `logs/launchd.out.log` / `logs/launchd.err.log`；Linux systemd 也可以用 `journalctl --user -u biaoge-bot.service -f` 查看服务日志。

调试 dump 和运行日志分开管理：

```env
BOT_LOG_LEVEL=INFO
CALLBACK_DUMP_ENABLED=0
SAVE_TASK_REQUEST_PARAMS=0
FEISHU_SEND_RESULT_TO_CHAT=0
```

`BOT_LOG_LEVEL` 只控制运行日志级别；`CALLBACK_DUMP_ENABLED=1` 会保存回调 payload 到 `logs/dumps/callbacks`；`SAVE_TASK_REQUEST_PARAMS=1` 会保存任务请求参数到 `logs/dumps/task_requests`。两个 dump 开关建议只在排查问题时临时开启。`FEISHU_SEND_RESULT_TO_CHAT=1` 时，绑定表格并回写的任务也会把生成结果同步发回触发的飞书对话框。

## 指令

参数写法（很重要）：

- 统一用“空格分隔”的 `key=value`（例如 `seed=1 steps=30`）
- 如果值里有空格，用引号包起来（现在支持）：例如 `prompt="hello world"`、`images="@./pics/my a.jpg"`
- 如果你想让机器人“先上传本机文件再执行”，在值前面加 `@`：例如 `images=@./pics/a.jpg`

- `/help`：查看帮助  
  示例：`/help`
- `/panel`：发送控制面板卡片  
  示例：`/panel`
- `/run_default`：运行默认工作流（workflows 里配置的 default_workflow）  
  示例：`/run_default`
- `/run ...`：运行默认工作流，并支持覆盖参数  
  示例：`/run seed=1 steps=30 prompt=hello`
- `/wf <workflowKey> ...`：运行指定 workflow key，并支持覆盖参数  
  示例：`/wf klein_add_real_details seed=1 steps=30 prompt=hello`  
  示例：`/wf klein_add_real_details 3.seed=1 10.text=hello`
- `/cb ...`：公网转发器触发用（正常不需要手动调用）  
  示例：`/cb provider=runninghub id=2058921669074444290 sig=xxxx`
- `/ids`：获取 chat_id / user_open_id  
  示例：`/ids`
- `/botid`：获取 bot_open_id（用于配置 FC 转发器的 FEISHU_AT_USER_ID）  
  示例：`/botid`

群聊里如果你希望“@ 机器人 + 指令”也能生效，写法可以是：

- `@机器人 /ids`
- `/ids @机器人`

## 示例（无表格读取）

### 1) 直接触发工作流（语义参数）

```text
/wf klein_add_real_details prompt=add_real_details，为图中的女人添加真实的照片细节
```

### 2) 直接写节点输入（不依赖 workflows 配置）

```text
/wf klein添加真实细节 2.image=openapi/xxxx.jpg 3.prompt=add_real_details，为图中的女人添加真实的照片细节
```

说明：

- ComfyUI：`2.image` 的值通常是 ComfyUI 可访问的 input 文件名（或你的工作流节点支持的路径形式）
- RunningHub：
  - 如果你的输入图本身有可访问的公网外链，可以直接把 URL 作为 `LoadImage.image`
  - 如果没有外链，则把图片上传到 RunningHub（`/openapi/v2/media/upload/binary`），并使用返回的 `fileName`（例如 `openapi/<hash>.png`）

## 公网转发器（阿里云 FC）

示例代码：`aliyun_fc_forwarder/handler.py`

用途：

- RunningHub / 私有云 ComfyUI 在公网回调到 FC
- FC 往群里发送 `/cb provider=... id=... sig=...` 并 @ 本地机器人触发处理

FC 环境变量（最低）：

- `FEISHU_APP_ID` / `FEISHU_APP_SECRET`
- `FEISHU_RECEIVE_ID`：群 chat_id（用 `/ids` 获取）
- `FEISHU_AT_USER_ID`：本地机器人的 bot_open_id（用 `/botid` 获取）
- `WEBHOOK_TOKEN`（可选，但推荐）
- `CB_MESSAGE_TOKEN`（可选，但推荐）

## 模式与能力

本仓库默认面向开源版（BITABLE_MODE=off）：

- 执行 → 回调 → 群消息交付（图片/文件/URL）

高级版（Pro）在此基础上增加多维表格/Drive 能力，可支持：

- 读取 → 执行（从多维表格读取字段与附件作为输入）
- 读取 → 执行 → 回填（状态/结果回写到多维表格）
- 执行 → 回填（不读表，但把输出写回指定记录）

## 高级版

高级版包含飞书多维表格/Drive 回写、表格队列跑批等能力。需要开通请加微信沟通（仓库不包含相关模块与授权文件）。

高级版说明文档：

- [README.pro.md](README.pro.md)

受保护模块编译与跨平台产物说明：

- [docs/protected_modules_build.md](docs/protected_modules_build.md)

微信二维码：

![wechat](assets/wechat.png)
