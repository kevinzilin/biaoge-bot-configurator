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
pip install -r requirements.txt
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
python -m biaoge_bot.main
```

## 指令

参数写法（很重要）：

- 统一用“空格分隔”的 `key=value`（例如 `seed=1 steps=30`）
- 如果值里有空格，建议不要用这种值（会被拆成多段，机器人读不懂）

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

微信二维码：

![wechat](data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAVsAAAFaCAYAAACwk/5IAAAAAXNSR0IArs4c6QAAAARnQU1BAACxjwv8YQUAAAAJcEhZcwAADsMAAA7DAcdvqGQAAP+lSURBVHhe7P0Hd1vXtS4Mnx/3jneMb9x7TxxLItErKdmOndhJ7NiOuwop9t4JEB0kAPbeOylSXXKRZElW72InOp5vzLnWBkGqMYlzcs59s+TpDWxu7L32Ks+aa9b/wBtKCkBiDyWTQCIp/ygpJYlOJ+VROaec589IIpWSpHzOPMffU+IevwLRM6nWqVQCSCWQos/0PEl0QUrSvop8b6VdogAi8hiXf+Y//gtKRnfsIqV/lOPe/nwV0fukX4U7XnlBcaBT/ClFbx8DUkT0q12//Hf5h8ruXkkiwf+Py38xxJFEFCmEkeI+oHFOM4h6gP7tjNIEIoghKn8VQ4J/q3SsLBkfX19o/sS4JnSfBN8vyWfTg4OGQYLm1p75/QLJ3+yhVAalv/Nc3fv7F+mFm8n5rvyjuvIYffGyF0j8gl8GKT4qRBdkTrTXl//Ye2JvoSrGdxEQTxGJbqTH0TEmH09HhTKrlT63j8lO12Te5x8herZo1Az0zUQi5SVku72xKJWUv8/sFy5Kg+yj8X/1shdlXzJwmLgZaNBmXJ6xMIqqi/5Nv0bmbzOaTpykya708r+yAf5vLJkDVEx7Ag0Bk0mGOwGYAmQz+2mHCKXE1QS2BI4EtgJ89vRVeiC/oSiDRs4p8U/eSakDV4smfFxQfM8xTQkglgCiSSCWSQQEDDYZRNfHXk9JWnQiLxKP0x1idMhE9JeSeC8FWrl50i+qfNkfg/FGsKVOFWvibgozJZm25fomuMZXE3ds5qR/HSklc9Bkltede9n5vfd/Ge2n7P0NkfJM+qyA8X7v92uWvXXKrEuaaILQck19Ibh9JuW7co45qJcPIeXWYq0XHI4g+vxvoP11S2YHin6heUTtTP/EvNvDTYqO2SH5XYA0cbVRCdW049vZ2f3NO7yMeytFQJP8Q4pqRxghdn+voygvA+FdFGNOnIg+E0XTO8nXEeG7APmdNkgldxaDNN/Ff9zLnu0lWph2M290651mojPbkl7fdm8EW558sdhuotWFSE5KIupEaozXUxyxVBKxVEoSfVZIOZdCnLYCchXPXGWUf9zTe8696lpqYqWB9jK2YryIv/P2Zx9FDMbMCbCH0nXY+8v/gsIvJ5+fWZe9deSJoGz7X0JyeIltodKOO6R8p39iS6sclXZlAc2/y69SlLGmHHeKwk/yUMsc1ETKd+Ucj33qpziSxOUmSVSXRDL5Iu2r8L0lyMuxLmaGwncToAs2jNd2eh4fFaLvCgHJ+Iuk8AGZ/AC/70sWiF2UTCKRiAtKCooTKQKPFBEjxq5mytzdKc1IfxDLktgDKLODmGzx2v8A2FJllQZPJAUQRlNJxOkFqLOSCSRIhCCBMQJB3Mj0Aq8j/kcdodDOa2SeE7KnOFJp+Z8g+s7nMs4r5153rXKWGksBiViKoJ+49DhWk1sIp4gze31jUaH3EB0WR5xWbuq4eATxRBTJJA2sOOJxWn//uYX6aC8XkqS+ScREPXjbKOCS60rvGo9iOx5GLEHbqBd3HgrF4mHRfpJTZS5KfleO9IxYLIwovTctkMr44IXyTcPu32X/JRNsheiHACr9mYY4z08CNzHPxFhUdhrUJzEkkjQuEvz3FMlR6S9yDGXSfsGWn5mivhdARr+KJOgczV7BfBEHzXNQ4Xb2sofMp5FogF6R0DS6D1KQMEM0+BJKv08GCUEKESOMlH6LM0Qkd96NT0R09SaSqU1EYpv8mzCBeHqMU2XobjTnXz/qd4FtPE4AJRo8kaDVL4mNWAxr/DhqwEzQ2nkMse4vrBKvoAzO/pW033vth15XaBBuJ7d4WGwktvbxCxIrJXi95i1LMoFIgjokhXgijkgswgM+Eo+9AIS/dlHuH4vFuK+UfuNJJAcXL4apJLYSMYRTVO8E1uMxXkC3CHR54Ugikq)
