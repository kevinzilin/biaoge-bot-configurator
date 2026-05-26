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
