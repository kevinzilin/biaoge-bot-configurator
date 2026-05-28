from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from .context import AppContext

_LOCK = threading.RLock()

_SENSITIVE_KEY_RE = re.compile(r"(SECRET|TOKEN|PASSWORD|PASS|API_KEY|APP_SECRET|PRIVATE)", re.IGNORECASE)
_SENSITIVE_KEYS = {
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "CB_MESSAGE_TOKEN",
    "RUNNINGHUB_API_KEY",
    "ADMIN_TOKEN",
}
_HIDDEN_ENV_KEYS = {
    "BITABLE_APP_TOKEN",
    "BITABLE_TABLE_ID",
    "BITABLE_APP_TOKEN_KLEIN",
    "BITABLE_TABLE_ID_KLEIN",
    "BITABLE_MODE",
    "BIAOGE_LICENSE_PATH",
    "WORKFLOW_CONFIG_PATH",
    "CALLBACK_PATH",
    "CALLBACK_DUMP_DIR",
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _env_path() -> Path:
    return _repo_root() / ".env"


def _workflow_path() -> Path:
    root = _repo_root()
    env_p = _env_path()
    try:
        _, values = _read_env_file(env_p)
        raw = str(values.get("WORKFLOW_CONFIG_PATH") or "").strip()
        raw = raw.strip('"').strip("'")
        if raw:
            p = Path(raw)
            if not p.is_absolute():
                p = (root / raw).resolve()
            else:
                p = p.resolve()
            try:
                p.relative_to(root.resolve())
                return p
            except Exception:
                pass
    except Exception:
        pass
    return root / "config" / "workflows.local.json"


def _is_sensitive_key(key: str) -> bool:
    k = (key or "").strip()
    if not k:
        return True
    if k in _SENSITIVE_KEYS:
        return True
    return bool(_SENSITIVE_KEY_RE.search(k))


def _is_hidden_env_key(key: str) -> bool:
    k = (key or "").strip()
    if not k:
        return True
    return k in _HIDDEN_ENV_KEYS


def _read_env_file(path: Path) -> tuple[list[str], dict[str, str]]:
    if not path.exists():
        return [], {}
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    values: dict[str, str] = {}
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        values[key] = val.strip()
    return lines, values


def _write_env_file(path: Path, updates: dict[str, str]) -> None:
    lines, existing = _read_env_file(path)
    existing_keys = set(existing.keys())

    def render_value(v: Any) -> str:
        if v is None:
            return ""
        return str(v)

    out_lines: list[str] = []
    for line in lines:
        if "=" not in line or line.lstrip().startswith("#"):
            out_lines.append(line)
            continue
        key, _ = line.split("=", 1)
        k = key.strip()
        if k in updates:
            out_lines.append(f"{k}={render_value(updates[k])}")
        else:
            out_lines.append(line)

    for k, v in updates.items():
        kk = str(k).strip()
        if not kk or kk in existing_keys:
            continue
        out_lines.append(f"{kk}={render_value(v)}")

    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    tmp.replace(path)


def _read_workflow_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8") or "{}")


def _write_workflow_config(path: Path, cfg: dict[str, Any]) -> None:
    bak = path.with_name(path.name + ".bak")
    if path.exists():
        bak.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _require_admin(req: Request) -> None:
    token = (os.environ.get("ADMIN_TOKEN") or "").strip()
    if token:
        provided = (req.headers.get("x-admin-token") or req.query_params.get("token") or "").strip()
        if provided != token:
            raise HTTPException(status_code=401, detail="unauthorized")
        return
    host = getattr(req.client, "host", "") if req.client else ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=401, detail="unauthorized")


def _reload_context_inplace(ctx: AppContext) -> None:
    from .main import build_context

    env_file = str(_env_path())
    new_ctx = build_context(env_file=env_file)
    with _LOCK:
        for k in (
            "settings",
            "config",
            "auth",
            "bitables",
            "drive",
            "comfyui",
            "workflows",
            "bitable_mode",
            "bitable_configs",
            "default_table_key",
            "default_workflow_key",
        ):
            object.__setattr__(ctx, k, getattr(new_ctx, k))
        try:
            ctx.runner.set_context(ctx)
        except Exception:
            pass


_PAGE_HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>biaoge 配置</title>
  <style>
    :root{
      --bg0:#070a12;
      --bg1:#0b1020;
      --card:rgba(255,255,255,0.06);
      --border:rgba(255,255,255,0.14);
      --text:#e5e7eb;
      --muted:#a3a3a3;
      --ok:#22c55e;
      --warn:#f59e0b;
      --err:#ef4444;
      --accent:#60a5fa;
      --accent2:#a78bfa;
      --shadow:0 10px 30px rgba(0,0,0,0.35);
    }
    html, body { height: 100%; overflow-x: hidden; }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      color: var(--text);
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Arial;
      background:
        radial-gradient(1200px 800px at 10% 10%, rgba(96,165,250,0.14), transparent 60%),
        radial-gradient(1000px 700px at 85% 20%, rgba(167,139,250,0.16), transparent 55%),
        radial-gradient(900px 700px at 55% 100%, rgba(34,197,94,0.08), transparent 55%),
        linear-gradient(180deg, var(--bg0), var(--bg1));
    }
    h1 { font-size: 18px; margin: 0; letter-spacing: 0.2px; }
    h2 { font-size: 14px; margin: 0; color: rgba(229,231,235,0.92); }
    .app { max-width: 1240px; margin: 0 auto; padding: 18px 16px 36px; }
    .muted { color: var(--muted); }
    .help { color: rgba(163,163,163,0.95); font-size: 12px; margin-top: 6px; line-height: 1.45; }
    .ok { color: var(--ok); }
    .warn { color: var(--warn); }
    .err { color: var(--err); }
    .card {
      border: 1px solid rgba(255,255,255,0.12);
      border-radius: 16px;
      padding: 14px;
      background: var(--card);
      box-shadow: var(--shadow);
      backdrop-filter: blur(10px);
      max-width: 100%;
      overflow: hidden;
    }
    .top {
      display: grid;
      grid-template-columns: auto minmax(240px, 1fr) auto auto auto;
      gap: 10px;
      align-items: center;
    }
    .layout {
      margin-top: 12px;
      display: grid;
      grid-template-columns: 300px minmax(0, 1fr);
      gap: 12px;
      align-items: start;
    }
    @media (max-width: 980px) {
      .top { grid-template-columns: 1fr; }
      .layout { grid-template-columns: 1fr; }
    }
    .pill {
      display: inline-flex;
      align-items: center;
      padding: 3px 10px;
      border-radius: 999px;
      background: rgba(96,165,250,0.18);
      border: 1px solid rgba(96,165,250,0.28);
      color: rgba(229,231,235,0.9);
      font-size: 12px;
      white-space: nowrap;
    }
    input[type="text"] {
      padding: 8px 10px;
      width: 100%;
      min-width: 0;
      color: var(--text);
      background: rgba(255,255,255,0.06);
      border: 1px solid var(--border);
      border-radius: 10px;
      outline: none;
    }
    input[type="text"]:focus { border-color: rgba(96,165,250,0.65); box-shadow: 0 0 0 3px rgba(96,165,250,0.15); }
    button {
      padding: 8px 10px;
      cursor: pointer;
      color: rgba(229,231,235,0.95);
      background: linear-gradient(135deg, rgba(96,165,250,0.22), rgba(167,139,250,0.18));
      border: 1px solid rgba(96,165,250,0.35);
      border-radius: 10px;
      white-space: nowrap;
    }
    button:hover { background: linear-gradient(135deg, rgba(96,165,250,0.28), rgba(167,139,250,0.22)); }
    .btnGhost { background: rgba(255,255,255,0.06); border: 1px solid var(--border); }
    .btnDanger { background: rgba(239,68,68,0.12); border: 1px solid rgba(239,68,68,0.35); }
    .side { padding: 12px; }
    .sideTitle { display:flex; align-items:center; justify-content: space-between; gap: 10px; margin: 10px 0 8px; }
    .sideTitle:first-child { margin-top: 0; }
    .sideList { display: grid; gap: 6px; }
    .sideItem {
      display:flex;
      align-items:center;
      justify-content: space-between;
      gap: 10px;
      padding: 8px 10px;
      border: 1px solid rgba(255,255,255,0.10);
      border-radius: 12px;
      background: rgba(255,255,255,0.04);
      cursor: pointer;
      width: 100%;
      text-align: left;
      appearance: none;
      -webkit-appearance: none;
    }
    .sideItem:hover { background: rgba(255,255,255,0.06); }
    .sideItem.active { border-color: rgba(96,165,250,0.45); background: rgba(96,165,250,0.10); }
    .sideKey { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .main { padding: 14px; }
    .sectionHeader { display:flex; align-items:center; justify-content: space-between; gap: 10px; margin-bottom: 12px; }
    .form { display: grid; gap: 12px; }
    .field { display: grid; gap: 6px; }
    .label { font-size: 12px; color: rgba(229,231,235,0.82); }
    .subBlock {
      border: 1px solid rgba(255,255,255,0.12);
      border-radius: 14px;
      padding: 10px;
      background: rgba(255,255,255,0.04);
      box-shadow: 0 8px 18px rgba(0,0,0,0.24);
      backdrop-filter: blur(10px);
      display: grid;
      gap: 10px;
    }
    .subHeader {
      display:flex;
      align-items:center;
      justify-content: space-between;
      gap: 10px;
      padding-bottom: 8px;
      border-bottom: 1px solid rgba(255,255,255,0.08);
    }
    .subTitle { font-size: 13px; color: rgba(229,231,235,0.92); font-weight: 650; }
    .relBody { display:grid; gap: 10px; }
    .row2 { display:grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    @media (max-width: 980px) { .row2 { grid-template-columns: 1fr; } }
    table { border-collapse: separate; border-spacing: 0; width: 100%; max-width: 100%; overflow: hidden; border-radius: 12px; table-layout: fixed; }
    th, td { padding: 10px 10px; vertical-align: top; border-bottom: 1px solid rgba(255,255,255,0.08); }
    th { text-align: left; color: rgba(229,231,235,0.9); background: rgba(255,255,255,0.06); font-weight: 600; }
    td { color: rgba(229,231,235,0.92); overflow-wrap: anywhere; word-break: break-word; }
    .kvWrap { display:grid; gap: 8px; }
    .kvActions { display:flex; gap: 10px; }
    .panelRows { display:grid; gap: 14px; margin-top: 12px; }
    .panelBtnList { display:grid; gap: 12px; }
    .panelBtnGrid { display:grid; grid-template-columns: 1fr 1.1fr 120px 90px; gap: 10px; align-items: center; }
    .panelBtnHeader { padding: 8px 10px; border-radius: 12px; background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.10); }
    .panelBtnHeader > div { font-size: 12px; color: rgba(229,231,235,0.90); font-weight: 650; }
    .panelBtnCard { border: 1px solid rgba(255,255,255,0.14); border-radius: 14px; padding: 10px; background: rgba(255,255,255,0.04); display:grid; gap: 10px; }
    @media (max-width: 980px) { .panelBtnGrid { grid-template-columns: 1fr; } }
    .bitableOnly { }
    .block {
      border: 1px solid rgba(255,255,255,0.12);
      border-radius: 16px;
      padding: 12px;
      background: rgba(255,255,255,0.05);
      box-shadow: 0 10px 24px rgba(0,0,0,0.28);
      backdrop-filter: blur(10px);
    }
    .blockTitle {
      display:flex;
      align-items:center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 10px;
    }
    .blockTitleLeft { display:flex; align-items:center; gap: 10px; min-width: 0; }
    .blockTitleText { font-size: 13px; color: rgba(229,231,235,0.92); font-weight: 650; white-space: nowrap; }
    .blockTitleSub { font-size: 12px; color: rgba(163,163,163,0.95); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .paramCard {
      border: 1px solid rgba(255,255,255,0.18) !important;
      background: rgba(255,255,255,0.072) !important;
      transition: background 0.22s ease, border-color 0.22s ease, box-shadow 0.22s ease, transform 0.22s ease;
    }
    .paramsBlock {
      background: rgba(255,255,255,0.082);
      border-color: rgba(255,255,255,0.20);
      box-shadow: 0 12px 26px rgba(0,0,0,0.28);
      transition: background 0.22s ease, border-color 0.22s ease, box-shadow 0.22s ease;
    }
    .paramsBlock.paramsStrictOn {
      background: rgba(255,255,255,0.028);
      border-color: rgba(255,255,255,0.10);
      box-shadow: 0 16px 34px rgba(0,0,0,0.40), 0 0 0 1px rgba(96,165,250,0.05) inset;
    }
    .paramsBlock.paramsStrictOn .paramCard {
      background: rgba(255,255,255,0.026) !important;
      border-color: rgba(255,255,255,0.10) !important;
      box-shadow: 0 10px 22px rgba(0,0,0,0.28);
      transform: translateY(-1px);
    }
    .switch {
      appearance: none;
      -webkit-appearance: none;
      width: 46px;
      height: 26px;
      border-radius: 999px;
      background: rgba(255,255,255,0.08);
      border: 1px solid rgba(255,255,255,0.18);
      position: relative;
      cursor: pointer;
      outline: none;
      transition: background 0.18s ease, border-color 0.18s ease, box-shadow 0.18s ease;
    }
    .switch::before {
      content: "";
      position: absolute;
      top: 2px;
      left: 2px;
      width: 22px;
      height: 22px;
      border-radius: 999px;
      background: rgba(229,231,235,0.92);
      box-shadow: 0 6px 16px rgba(0,0,0,0.35);
      transition: transform 0.18s ease, background 0.18s ease;
    }
    .switch:focus { box-shadow: 0 0 0 3px rgba(96,165,250,0.15); border-color: rgba(96,165,250,0.65); }
    .switch:checked { background: rgba(34,197,94,0.22); border-color: rgba(34,197,94,0.55); }
    .switch:checked::before { transform: translateX(20px); background: rgba(255,255,255,0.95); }
    textarea {
      padding: 8px 10px;
      width: 100%;
      min-width: 0;
      color: var(--text);
      background: rgba(255,255,255,0.06);
      border: 1px solid var(--border);
      border-radius: 10px;
      outline: none;
      resize: vertical;
      line-height: 1.35;
    }
    textarea:focus { border-color: rgba(96,165,250,0.65); box-shadow: 0 0 0 3px rgba(96,165,250,0.15); }
  </style>
</head>
<body>
  <div class="app">
    <div class="card">
      <div class="top">
        <h1>biaoge 配置台</h1>
        <input id="token" type="text" placeholder="ADMIN_TOKEN（可从 URL ?token= 自动读取）" />
        <button id="btnReloadAll" class="btnGhost">重新拉取</button>
        <button id="btnSaveAll">保存并重载</button>
        <span id="status" class="muted"></span>
      </div>
      <div class="help">说明：密钥类参数不会展示在页面；页面只提供表单化编辑，避免直接改 JSON 造成格式错误。</div>
    </div>

    <div class="layout">
      <div class="card side">
        <div class="sideTitle">
          <h2>.env</h2>
          <span class="muted" id="envCount"></span>
        </div>
        <div id="envList" class="sideList"></div>

        <div class="sideTitle" style="margin-top:14px">
          <h2>global</h2>
        </div>
        <div id="globalList" class="sideList"></div>

        <div class="sideTitle bitableOnly" id="tablesTitle" style="margin-top:14px">
          <h2>tables</h2>
          <button id="btnAddTable" class="btnGhost">新增</button>
        </div>
        <div id="tableList" class="sideList bitableOnly"></div>

        <div class="sideTitle" style="margin-top:14px">
          <h2>workflows</h2>
          <button id="btnAddWorkflow" class="btnGhost">新增</button>
        </div>
        <div id="wfPath" class="muted" style="margin:-6px 0 8px 2px; font-size:12px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;"></div>
        <div id="workflowList" class="sideList"></div>
      </div>

      <div class="card main">
        <div id="editor"></div>
      </div>
    </div>
  </div>

<script>
  const $ = (id) => document.getElementById(id);
  const ENV_DESC = {
    "BOT_LOG_LEVEL": "日志等级（INFO/DEBUG/WARNING/ERROR）。排查问题时可临时改为 DEBUG。",
    "CALLBACK_HOST": "本机/局域网可访问的监听地址。示例：127.0.0.1（仅本机）或 192.168.x.x（局域网可访问）。",
    "CALLBACK_PORT": "回调服务端口，配置页也是通过该端口访问。",
    "COMFYUI_BASE_URL": "ComfyUI 服务地址（示例：http://127.0.0.1:8188 或远程地址）。",
    "COMFYUI_INPUT_DIR": "ComfyUI 输入目录（可选）。留空表示不指定。",
    "COMFYUI_UPLOAD_ENABLED": "是否允许上传图片到 ComfyUI（1/0）。",
    "COMFYUI_UPLOAD_SUBFOLDER": "上传到 ComfyUI 的子目录（可选）。",
    "COMFYUI_UPLOAD_OVERWRITE": "上传同名文件时是否覆盖（true/false）。",
    "BITABLE_DOWNLOAD_DIR": "多维表格/附件下载的本地目录。",
    "REMOTE_CALLBACK_URL": "公网/远程回调地址（可选）。适用于外部服务能回调到你指定的地址的情况。",
    "REMOTE_RESULT_MODE": "远程结果获取模式（例如 poll/fc）。",
    "REMOTE_POLL_INTERVAL_SECONDS": "远程轮询间隔（秒），过小可能导致请求过频。",
    "REMOTE_POLL_FALLBACK_SECONDS": "远程轮询兜底超时（秒），超过将走兜底策略。",
  };
  const STATE = {
    env: {},
    envMeta: {},
    cfg: {},
    selected: {type: "env", key: ""},
  };
  const PARAM_TYPE_OPTIONS = ["str", "int", "float", "bool"];
  const TABLE_FIELD_KEY_OPTIONS = ["status", "output", "error", "prompt_id", "created_time", "trigger_cmd", "trigger_user"];
  const STATUS_VALUE_KEY_OPTIONS = ["queued", "trigger", "running", "done", "partial", "failed"];
  let CURRENT = null;

  window.addEventListener("error", (ev) => {
    try {
      const msg = ev && ev.message ? String(ev.message) : "unknown error";
      setStatus("页面脚本错误：" + msg, "err");
    } catch (e) {}
  });
  window.addEventListener("unhandledrejection", (ev) => {
    try {
      const msg = ev && ev.reason ? String(ev.reason && ev.reason.message ? ev.reason.message : ev.reason) : "unknown rejection";
      setStatus("页面请求错误：" + msg, "err");
    } catch (e) {}
  });

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) for (const [k,v] of Object.entries(attrs)) {
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k === "html") node.innerHTML = v;
      else node.setAttribute(k, v);
    }
    if (children) for (const c of children) node.appendChild(c);
    return node;
  }

  function ensureParamTypeDatalist() {
    let dl = document.getElementById("paramTypeDatalist");
    if (dl) return dl;
    dl = el("datalist", {id: "paramTypeDatalist"});
    for (const t of PARAM_TYPE_OPTIONS) dl.appendChild(el("option", {value: t}));
    document.body.appendChild(dl);
    return dl;
  }

  function ensureSimpleDatalist(id, items) {
    let dl = document.getElementById(id);
    if (dl) return dl;
    dl = el("datalist", {id});
    for (const it of (Array.isArray(items) ? items : [])) {
      dl.appendChild(el("option", {value: String(it || "")}));
    }
    document.body.appendChild(dl);
    return dl;
  }

  function setStatus(text, cls) {
    const el0 = $("status");
    el0.className = cls || "muted";
    el0.textContent = text || "";
  }

  function authHeaders() {
    const url = new URL(location.href);
    const q = url.searchParams.get("token");
    const t = ($("token").value || q || "").trim();
    try {
      if (t) localStorage.setItem("admin_token", t);
    } catch (e) {}
    return t ? {"x-admin-token": t} : {};
  }

  function deepCopy(v) {
    return JSON.parse(JSON.stringify(v || {}));
  }

  function kvEditor(initial, keyPlaceholder, valuePlaceholder, addText, options) {
    const wrap = el("div", {class:"kvWrap"});
    const table = el("table");
    const opts = (options && typeof options === "object") ? options : {};
    const keyListId = (opts.keyListId && String(opts.keyListId || "").trim()) ? String(opts.keyListId || "").trim() : "";
    const keyOptions = Array.isArray(opts.keyOptions) ? opts.keyOptions : [];
    const valueListId = (opts.valueListId && String(opts.valueListId || "").trim()) ? String(opts.valueListId || "").trim() : "";
    const valueOptions = Array.isArray(opts.valueOptions) ? opts.valueOptions : [];
    if (keyListId && keyOptions.length) ensureSimpleDatalist(keyListId, keyOptions);
    if (valueListId && valueOptions.length) ensureSimpleDatalist(valueListId, valueOptions);
    const thead = el("thead", null, [el("tr", null, [
      el("th", {text:"KEY", style:"width:220px"}),
      el("th", {text:"VALUE"}),
      el("th", {text:"", style:"width:90px"})
    ])]);
    const tbody = el("tbody");
    table.appendChild(thead);
    table.appendChild(tbody);

    function addRow(k, v) {
      const key = el("input", {type:"text", value: (k||""), placeholder: keyPlaceholder || ""});
      const val = el("input", {type:"text", value: (v==null ? "" : String(v)), placeholder: valuePlaceholder || ""});
      if (keyListId) key.setAttribute("list", keyListId);
      if (valueListId) val.setAttribute("list", valueListId);
      const del = el("button", {type:"button", text:"删除", class:"btnGhost"});
      const tr = el("tr", null, [
        el("td", null, [key]),
        el("td", null, [val]),
        el("td", null, [del]),
      ]);
      del.onclick = () => tr.remove();
      tbody.appendChild(tr);
    }

    const initObj = initial && typeof initial === "object" ? initial : {};
    const keys = Object.keys(initObj);
    if (keys.length) {
      for (const k of keys) addRow(k, initObj[k]);
    } else {
      addRow("", "");
    }

    const addBtn = el("button", {type:"button", text:(addText || "新增一行"), class:"btnGhost"});
    addBtn.onclick = () => addRow("", "");
    wrap.appendChild(table);
    wrap.appendChild(el("div", {class:"kvActions"}, [addBtn]));
    wrap._getValue = () => {
      const out = {};
      for (const tr of Array.from(tbody.querySelectorAll("tr"))) {
        const inputs = tr.querySelectorAll("input");
        const k = (inputs[0].value || "").trim();
        const v = (inputs[1].value || "").trim();
        if (!k) continue;
        out[k] = v;
      }
      return out;
    };
    return wrap;
  }

  function listEditor(initialList, valuePlaceholder) {
    const wrap = el("div", {class:"kvWrap"});
    const table = el("table");
    const tbody = el("tbody");
    table.appendChild(tbody);

    function addRow(v) {
      const val = el("input", {type:"text", value: (v==null ? "" : String(v)), placeholder: valuePlaceholder || ""});
      const del = el("button", {type:"button", text:"删除", class:"btnGhost"});
      const tr = el("tr", null, [
        el("td", null, [val]),
        el("td", null, [del]),
      ]);
      del.onclick = () => tr.remove();
      tbody.appendChild(tr);
    }

    const init = Array.isArray(initialList) ? initialList : (typeof initialList === "string" ? [initialList] : []);
    if (init.length) {
      for (const v of init) addRow(v);
    } else {
      addRow("");
    }

    const addBtn = el("button", {type:"button", text:"新增字段", class:"btnGhost"});
    addBtn.onclick = () => addRow("");
    wrap.appendChild(table);
    wrap.appendChild(el("div", {class:"kvActions"}, [addBtn]));
    wrap._getValue = () => {
      const out = [];
      for (const tr of Array.from(tbody.querySelectorAll("tr"))) {
        const input = tr.querySelector("input");
        const v = (input && input.value ? String(input.value) : "").trim();
        if (!v) continue;
        out.push(v);
      }
      return out;
    };
    return wrap;
  }

  function targetsEditor(initialTargets) {
    const wrap = el("div", {class:"kvWrap"});
    const table = el("table");
    const thead = el("thead", null, [el("tr", null, [
      el("th", {text:"nodeId", style:"width:140px"}),
      el("th", {text:"fieldName", style:"width:220px"}),
      el("th", {text:"index(可选)", style:"width:140px"}),
      el("th", {text:"", style:"width:90px"}),
    ])]);
    const tbody = el("tbody");
    table.appendChild(thead);
    table.appendChild(tbody);

    function addRow(t) {
      const nodeId = el("input", {type:"text", value: (t && t.nodeId!=null ? String(t.nodeId) : "")});
      const fieldName = el("input", {type:"text", value: (t && t.fieldName!=null ? String(t.fieldName) : "")});
      const index = el("input", {type:"text", value: (t && t.index!=null ? String(t.index) : "")});
      const del = el("button", {type:"button", text:"删除", class:"btnGhost"});
      const tr = el("tr", null, [
        el("td", null, [nodeId]),
        el("td", null, [fieldName]),
        el("td", null, [index]),
        el("td", null, [del]),
      ]);
      del.onclick = () => tr.remove();
      tbody.appendChild(tr);
    }

    const init = Array.isArray(initialTargets) ? initialTargets : [];
    if (init.length) {
      for (const t of init) addRow(t);
    } else {
      addRow({});
    }

    const addBtn = el("button", {type:"button", text:"新增 target", class:"btnGhost"});
    addBtn.onclick = () => addRow({});
    wrap.appendChild(table);
    wrap.appendChild(el("div", {class:"kvActions"}, [addBtn]));
    wrap._getValue = () => {
      const out = [];
      for (const tr of Array.from(tbody.querySelectorAll("tr"))) {
        const inputs = tr.querySelectorAll("input");
        const nodeId = (inputs[0].value || "").trim();
        const fieldName = (inputs[1].value || "").trim();
        const indexRaw = (inputs[2].value || "").trim();
        if (!nodeId || !fieldName) continue;
        const item = {nodeId, fieldName};
        if (indexRaw) {
          const n = parseInt(indexRaw, 10);
          if (!isNaN(n)) item.index = n;
        }
        out.push(item);
      }
      return out;
    };
    return wrap;
  }

  function paramsEditor(initialParams) {
    const wrap = el("div", {class:"kvWrap"});
    const list = el("div", {class:"kvWrap"});

    function addParam(key, spec) {
      const orig = spec && typeof spec === "object" ? spec : {};
      const card = el("div", {class:"card paramCard", style:"padding:12px"});
      const head = el("div", {class:"sectionHeader"}, [
        el("span", {class:"pill", text:"param"}),
        el("input", {type:"text", value: key || "", placeholder:"参数名(如 prompt)"}),
        el("button", {type:"button", text:"删除", class:"btnDanger"})
      ]);
      const nameInput = head.querySelector("input");
      const delBtn = head.querySelector("button");
      delBtn.onclick = () => card.remove();

      ensureParamTypeDatalist();
      const typeInput = el("input", {type:"text", list:"paramTypeDatalist", value: String(orig.type || ""), placeholder:"例如：str / int / float / bool"});
      const targets = targetsEditor(orig.targets || []);
      const form = el("div", {class:"form"}, [
        el("div", {class:"field"}, [el("div", {class:"label", text:"type"}), typeInput, el("div", {class:"help", text:"可选值：str / int / float / bool。不填默认按 str 处理。"} )]),
        el("div", {class:"field"}, [el("div", {class:"label", text:"targets"}), targets, el("div", {class:"help", text:"targets 用于把参数写入工作流节点：nodeId 为节点编号，fieldName 为字段名，index 仅用于数组位置（可空）。"})]),
      ]);
      card.appendChild(head);
      card.appendChild(form);
      card._collect = (strictEmptyName) => {
        const name = (nameInput.value || "").trim();
        const hasType = !!(typeInput.value || "").trim();
        const targetItems = targets._getValue();
        const hasTargets = Array.isArray(targetItems) && targetItems.length > 0;
        const hasMeaningfulContent = hasType || hasTargets;
        if (!name) {
          if (!hasMeaningfulContent) return null;
          if (strictEmptyName) throw new Error("params 里有参数名为空");
          return null;
        }
        const out = {...orig};
        const t = (typeInput.value || "").trim();
        if (t) out.type = t; else delete out.type;
        out.targets = targetItems;
        return [name, out];
      };
      list.appendChild(card);
    }

    const init = initialParams && typeof initialParams === "object" ? initialParams : {};
    const keys = Object.keys(init);
    if (keys.length) {
      for (const k of keys) addParam(k, init[k]);
    } else {
      addParam("", {});
    }

    const addBtn = el("button", {type:"button", text:"新增参数", class:"btnGhost"});
    addBtn.onclick = () => addParam("", {});
    wrap.appendChild(el("div", {class:"help", text:"params 用来定义可从表格/命令传入的参数，以及每个参数要写入工作流的哪些节点。"}));
    wrap.appendChild(list);
    wrap.appendChild(addBtn);
    wrap._setStrictTone = (enabled) => {
      if (enabled) wrap.classList.add("paramsStrictOn");
      else wrap.classList.remove("paramsStrictOn");
    };
    wrap._getValue = (options) => {
      const opts = (options && typeof options === "object") ? options : {};
      const strictEmptyName = !!opts.strictEmptyName;
      const out = {};
      for (const card of Array.from(list.children)) {
        const pair = card._collect(strictEmptyName);
        if (!pair) continue;
        const [k, v] = pair;
        out[k] = v;
      }
      return out;
    };
    return wrap;
  }

  function applyBitableMode() {
    const mode = String(STATE.envMeta.bitable_mode || "").trim().toLowerCase();
    const enabled = mode !== "off";
    const items = document.querySelectorAll(".bitableOnly");
    for (const it of items) it.style.display = enabled ? "" : "none";
    if (!enabled && STATE.selected.type === "table") {
      const first = Object.keys(STATE.env || {}).sort()[0] || "";
      select("env", first);
    }
  }

  function commitCurrent() {
    if (!CURRENT || !CURRENT.commit) return true;
    try {
      CURRENT.commit();
      CURRENT = null;
      return true;
    } catch (e) {
      setStatus("当前配置有错误：" + String(e && e.message ? e.message : e), "err");
      return false;
    }
  }

  function select(type, key) {
    if (!commitCurrent()) return;
    STATE.selected = {type, key};
    renderSidebar();
    renderEditor();
  }

  function renderSidebar() {
    const envList = $("envList");
    envList.innerHTML = "";
    const keys = Object.keys(STATE.env || {}).sort();
    $("envCount").textContent = keys.length ? (keys.length + " 项") : "";
    for (const k of keys) {
      const active = STATE.selected.type === "env" && STATE.selected.key === k;
      const item = el("button", {type:"button", class:"sideItem" + (active ? " active" : "")}, [
        el("div", {class:"sideKey", text:k}),
      ]);
      item.onclick = () => select("env", k);
      envList.appendChild(item);
    }

    const globalList = $("globalList");
    globalList.innerHTML = "";
    {
      const active = STATE.selected.type === "global";
      const item = el("button", {type:"button", class:"sideItem" + (active ? " active" : "")}, [
        el("div", {class:"sideKey", text:"default_table / default_workflow"}),
      ]);
      item.onclick = () => select("global", "root");
      globalList.appendChild(item);
    }
    {
      const active = STATE.selected.type === "panel";
      const item = el("button", {type:"button", class:"sideItem" + (active ? " active" : "")}, [
        el("div", {class:"sideKey", text:"panel"}),
      ]);
      item.onclick = () => select("panel", "root");
      globalList.appendChild(item);
    }
    {
      const active = STATE.selected.type === "automation";
      const item = el("button", {type:"button", class:"sideItem" + (active ? " active" : "")}, [
        el("div", {class:"sideKey", text:"automation"}),
      ]);
      item.onclick = () => select("automation", "root");
      globalList.appendChild(item);
    }

    const tables = (STATE.cfg && STATE.cfg.tables) ? STATE.cfg.tables : {};
    const tableList = $("tableList");
    tableList.innerHTML = "";
    for (const k of Object.keys(tables || {}).sort()) {
      const active = STATE.selected.type === "table" && STATE.selected.key === k;
      const item = el("button", {type:"button", class:"sideItem" + (active ? " active" : "")}, [
        el("div", {class:"sideKey", text:k}),
      ]);
      item.onclick = () => select("table", k);
      tableList.appendChild(item);
    }

    const wfs = (STATE.cfg && STATE.cfg.workflows) ? STATE.cfg.workflows : {};
    const wfList = $("workflowList");
    wfList.innerHTML = "";
    for (const k of Object.keys(wfs || {}).sort()) {
      const active = STATE.selected.type === "workflow" && STATE.selected.key === k;
      const item = el("button", {type:"button", class:"sideItem" + (active ? " active" : "")}, [
        el("div", {class:"sideKey", text:k}),
      ]);
      item.onclick = () => select("workflow", k);
      wfList.appendChild(item);
    }
    applyBitableMode();
  }

  function renderEditor() {
    const root = $("editor");
    root.innerHTML = "";
    const t = STATE.selected.type;
    const key = STATE.selected.key;

    if (t === "global") {
      const tables = (STATE.cfg && STATE.cfg.tables) ? STATE.cfg.tables : {};
      const wfs = (STATE.cfg && STATE.cfg.workflows) ? STATE.cfg.workflows : {};
      const currentDefaultTable = String((STATE.cfg && (STATE.cfg.default_table || STATE.cfg.defaultTable)) || "");
      const currentDefaultWorkflow = String((STATE.cfg && (STATE.cfg.default_workflow || STATE.cfg.defaultWorkflow)) || "");

      const defaultTable = el("input", {type:"text", value: currentDefaultTable});
      const defaultWorkflow = el("input", {type:"text", value: currentDefaultWorkflow});

      const tableDataListId = "dl_default_table";
      const wfDataListId = "dl_default_workflow";
      defaultTable.setAttribute("list", tableDataListId);
      defaultWorkflow.setAttribute("list", wfDataListId);

      const dlTable = el("datalist", {id: tableDataListId}, Object.keys(tables || {}).sort().map(k2 => el("option", {value: k2})));
      const dlWf = el("datalist", {id: wfDataListId}, Object.keys(wfs || {}).sort().map(k2 => el("option", {value: k2})));

      root.appendChild(el("div", {class:"sectionHeader"}, [
        el("div", null, [el("h2", {text:"global"}), el("div", {class:"help", text:"配置默认表格与默认工作流。它们会影响 /run_default、以及未指定 table/workflow 的情况。"})]),
      ]));

      root.appendChild(el("div", {class:"form"}, [
        el("div", {class:"field"}, [el("div", {class:"label", text:"default_table"}), defaultTable, el("div", {class:"help", text:"默认表格 key（对应 tables）。"})]),
        el("div", {class:"field"}, [el("div", {class:"label", text:"default_workflow"}), defaultWorkflow, el("div", {class:"help", text:"默认工作流 key（对应 workflows）。"})]),
      ]));

      root.appendChild(dlTable);
      root.appendChild(dlWf);

      CURRENT = {
        commit: () => {
          const dt = (defaultTable.value || "").trim();
          const dw = (defaultWorkflow.value || "").trim();
          if (dt) STATE.cfg.default_table = dt; else delete STATE.cfg.default_table;
          if (dw) STATE.cfg.default_workflow = dw; else delete STATE.cfg.default_workflow;
        }
      };
      return;
    }

    if (t === "panel") {
      const orig = (STATE.cfg && STATE.cfg.panel && typeof STATE.cfg.panel === "object") ? STATE.cfg.panel : {};
      const titleInput = el("input", {type:"text", value: String(orig.title || "ComfyUI 控制面板")});

      const rowsWrap = el("div", {class:"panelRows"}, []);

      function makeTypeSelect(value) {
        const sel = el("select", null, [
          el("option", {value:"default", text:"default"}),
          el("option", {value:"primary", text:"primary"}),
          el("option", {value:"danger", text:"danger"}),
        ]);
        const v = String(value || "default");
        sel.value = (v === "primary" || v === "danger" || v === "default") ? v : "default";
        return sel;
      }

      function addButton(btnList, btn) {
        const text = el("input", {type:"text", value: String((btn && btn.text) || ""), placeholder:"按钮文字"});
        const cmd = el("input", {type:"text", value: String((btn && btn.cmd) || ""), placeholder:"cmd（如 help / run_default / drain）"});
        const typeSel = makeTypeSelect(btn && btn.type);
        const argsEd = kvEditor((btn && btn.args) || {}, "参数 key", "参数 value", "新增参数");
        const del = el("button", {type:"button", text:"删除", class:"btnGhost"});

        const top = el("div", {class:"panelBtnGrid"}, [text, cmd, typeSel, del]);
        const card = el("div", {class:"panelBtnCard"}, [top, argsEd]);

        del.onclick = () => card.remove();
        card._getValue = () => {
          const t1 = (text.value || "").trim();
          const c1 = (cmd.value || "").trim();
          const tp = (typeSel.value || "default").trim();
          const args = argsEd._getValue ? argsEd._getValue() : {};
          return {text: t1, cmd: c1, type: tp, args: args};
        };
        btnList.appendChild(card);
      }

      function addRow(row) {
        const wrap = el("div", {class:"block"});
        const header = el("div", {class:"blockTitle"}, [
          el("div", {class:"blockTitleLeft"}, [
            el("span", {class:"pill", text:"面板"}),
            el("div", {class:"blockTitleText", text:"一行按钮"}),
            el("div", {class:"blockTitleSub", text:"这一行会放在同一行显示"}),
          ]),
        ]);
        const delRow = el("button", {type:"button", text:"删除本行", class:"btnDanger", style:"float:right"});
        header.appendChild(delRow);
        delRow.onclick = () => wrap.remove();
        const btnHeader = el("div", {class:"panelBtnGrid panelBtnHeader"}, [
          el("div", {text:"文字"}),
          el("div", {text:"cmd"}),
          el("div", {text:"type"}),
          el("div", {text:""}),
        ]);
        const btnList = el("div", {class:"panelBtnList"}, []);

        const init = Array.isArray(row) ? row : [];
        if (init.length) {
          for (const b of init) addButton(btnList, b);
        } else {
          addButton(btnList, {text:"", cmd:"", type:"default", args:{}});
        }

        const addBtn = el("button", {type:"button", text:"新增按钮", class:"btnGhost"});
        addBtn.onclick = () => addButton(btnList, {text:"", cmd:"", type:"default", args:{}});

        wrap.appendChild(header);
        wrap.appendChild(el("div", {class:"form"}, [
          el("div", {class:"field"}, [
            el("div", {class:"label", text:"按钮列表"}),
            btnHeader,
            btnList,
            el("div", {class:"help", text:"cmd 对应你的指令名；args 会原样传给指令。"}),
          ]),
          el("div", {class:"kvActions", style:"margin-top:10px"}, [addBtn]),
        ]));

        wrap._getValue = () => {
          const out = [];
          for (const el0 of Array.from(btnList.children)) {
            if (!el0._getValue) continue;
            const v = el0._getValue();
            if (!v || !v.text || !v.cmd) continue;
            out.push(v);
          }
          return out;
        };
        rowsWrap.appendChild(wrap);
      }

      const origRows = Array.isArray(orig.rows) ? orig.rows : null;
      if (origRows && origRows.length) {
        for (const r of origRows) addRow(r);
      } else {
        addRow([{text:"运行默认流程", cmd:"run_default", type:"primary", args:{}}, {text:"执行队列(drain)", cmd:"drain", type:"danger", args:{}}]);
      }

      const addRowBtn = el("button", {type:"button", text:"新增一排按钮", class:"btnGhost"});
      addRowBtn.onclick = () => addRow([]);

      root.appendChild(el("div", {class:"sectionHeader"}, [
        el("div", null, [el("h2", {text:"panel"}), el("div", {class:"help", text:"配置 /panel 发送的控制面板按钮。每一行代表一排按钮。"})]),
      ]));
      root.appendChild(el("div", {class:"form"}, [
        el("div", {class:"field"}, [el("div", {class:"label", text:"title"}), titleInput, el("div", {class:"help", text:"面板标题。"})]),
      ]));
      root.appendChild(rowsWrap);
      root.appendChild(el("div", {class:"kvActions", style:"margin-top:12px"}, [addRowBtn]));

      CURRENT = {
        commit: () => {
          const title = (titleInput.value || "").trim() || "ComfyUI 控制面板";
          const rows = [];
          for (const rowEl of Array.from(rowsWrap.children)) {
            if (!rowEl._getValue) continue;
            const r = rowEl._getValue();
            if (Array.isArray(r) && r.length) rows.push(r);
          }
          STATE.cfg.panel = {title, rows};
        }
      };
      return;
    }

    if (t === "automation") {
      const orig = (STATE.cfg && STATE.cfg.automation && typeof STATE.cfg.automation === "object") ? STATE.cfg.automation : {};
      const triggerCmdField = el("input", {
        type:"text",
        value: String(orig.trigger_cmd_field || orig.triggerCmdField || orig.trigger_field || orig.triggerField || orig.cmd_field || orig.cmdField || ""),
        placeholder:"例如：触发指令",
      });
      const triggerUserField = el("input", {
        type:"text",
        value: String(orig.trigger_user_field || orig.triggerUserField || orig.operator_field || orig.operatorField || ""),
        placeholder:"例如：触发人（可选）",
      });

      root.appendChild(el("div", {class:"sectionHeader"}, [
        el("div", null, [el("h2", {text:"automation"}), el("div", {class:"help", text:"配置表格事件触发的全局兜底字段名。优先级是：table.fields 里的单表配置优先，automation 只在表里没写时才会生效。"})]),
      ]));

      root.appendChild(el("div", {class:"form"}, [
        el("div", {class:"block bitableOnly"}, [
          el("div", {class:"blockTitle"}, [
            el("div", {class:"blockTitleLeft"}, [
              el("span", {class:"pill", text:"触发"}),
              el("div", {class:"blockTitleText", text:"automation"}),
              el("div", {class:"blockTitleSub", text:"表格触发执行的全局默认字段"}),
            ]),
          ]),
          el("div", {class:"form"}, [
            el("div", {class:"field"}, [
              el("div", {class:"label", text:"trigger_cmd_field"}),
              triggerCmdField,
              el("div", {class:"help", text:"全局默认的“触发指令”列名。只有 tables.xxx.fields 没写 trigger_cmd 时，才会退回用这里。"}),
            ]),
            el("div", {class:"field"}, [
              el("div", {class:"label", text:"trigger_user_field（可选）"}),
              triggerUserField,
              el("div", {class:"help", text:"全局默认的“触发人”列名。只有事件里拿不到 operator_open_id，且 tables.xxx.fields 没写 trigger_user 时，才会退回用这里。推荐使用人员字段；留空表示不用这条兜底链路。"}),
            ]),
          ]),
        ]),
      ]));

      CURRENT = {
        commit: () => {
          const out = deepCopy(orig);
          const cmdField = (triggerCmdField.value || "").trim();
          const userField = (triggerUserField.value || "").trim();
          if (cmdField) out.trigger_cmd_field = cmdField; else delete out.trigger_cmd_field;
          delete out.triggerCmdField;
          delete out.trigger_field;
          delete out.triggerField;
          delete out.cmd_field;
          delete out.cmdField;
          if (userField) out.trigger_user_field = userField; else delete out.trigger_user_field;
          delete out.triggerUserField;
          delete out.operator_field;
          delete out.operatorField;
          if (Object.keys(out).length) STATE.cfg.automation = out;
          else delete STATE.cfg.automation;
        }
      };
      return;
    }

    if (t === "env") {
      const v = (STATE.env && key in STATE.env) ? String(STATE.env[key] || "") : "";
      const desc = ENV_DESC[key] || "配置说明待补充。";
      const input = el("input", {type:"text", value: v});
      const header = el("div", {class:"sectionHeader"}, [
        el("div", null, [el("h2", {text: key || ".env"}), el("div", {class:"help", text: desc})]),
      ]);
      const form = el("div", {class:"form"}, [
        el("div", {class:"field"}, [el("div", {class:"label", text:"值"}), input]),
      ]);
      root.appendChild(header);
      root.appendChild(form);
      CURRENT = {
        commit: () => {
          if (!key) return;
          STATE.env[key] = input.value;
        }
      };
      return;
    }

    if (t === "table") {
      const mode = String(STATE.envMeta.bitable_mode || "").trim().toLowerCase();
      if (mode === "off") {
        root.appendChild(el("div", {class:"help", text:"BITABLE_MODE=off：已隐藏 tables 配置。"}));
        return;
      }
      const tables = STATE.cfg.tables || {};
      const orig = tables[key] || {};
      const keyInput = el("input", {type:"text", value: key || "", placeholder:"table key"});
      const delBtn = el("button", {type:"button", text:"删除 table", class:"btnDanger"});
      const appToken = el("input", {type:"text", value: String(orig.app_token || "")});
      const tableId = el("input", {type:"text", value: String(orig.table_id || "")});
      const viewId = el("input", {type:"text", value: String(orig.view_id || "")});
      const fields = kvEditor(
        orig.fields || {},
        "字段key(如 status)",
        "列名(如 任务状态)",
        undefined,
        {keyListId:"tableFieldKeyDatalist", keyOptions: TABLE_FIELD_KEY_OPTIONS}
      );
      const statusValues = kvEditor(
        orig.status_values || {},
        "状态key(如 queued)",
        "显示值(如 待处理)",
        undefined,
        {keyListId:"statusValueKeyDatalist", keyOptions: STATUS_VALUE_KEY_OPTIONS}
      );

      delBtn.onclick = () => {
        delete tables[key];
        STATE.selected = {type:"env", key: Object.keys(STATE.env||{}).sort()[0] || ""};
        renderSidebar();
        renderEditor();
      };

      root.appendChild(el("div", {class:"sectionHeader"}, [
        el("div", null, [el("h2", {text:"table: " + (key || "")}), el("div", {class:"help", text:"配置飞书多维表格信息与字段映射。"})]),
        delBtn,
      ]));
      root.appendChild(el("div", {class:"form"}, [
        el("div", {class:"block"}, [
          el("div", {class:"blockTitle"}, [
            el("div", {class:"blockTitleLeft"}, [
              el("span", {class:"pill", text:"基础"}),
              el("div", {class:"blockTitleText", text:"tableBase"}),
              el("div", {class:"blockTitleSub", text:"表格 key / app_token / table_id / view_id"}),
            ]),
          ]),
          el("div", {class:"form"}, [
            el("div", {class:"field"}, [el("div", {class:"label", text:"table key"}), keyInput, el("div", {class:"help", text:"用于在 workflows 中引用该表格（例如 targetTableKey）。"})]),
            el("div", {class:"row2"}, [
              el("div", {class:"field"}, [el("div", {class:"label", text:"app_token"}), appToken, el("div", {class:"help", text:"飞书多维表格 app_token。"})]),
              el("div", {class:"field"}, [el("div", {class:"label", text:"table_id"}), tableId, el("div", {class:"help", text:"飞书多维表格 table_id（表格 ID）。"})]),
            ]),
            el("div", {class:"field"}, [el("div", {class:"label", text:"view_id（可选）"}), viewId, el("div", {class:"help", text:"不填表示默认视图。"})]),
          ]),
        ]),
        el("div", {class:"block"}, [
          el("div", {class:"blockTitle"}, [
            el("div", {class:"blockTitleLeft"}, [
              el("span", {class:"pill", text:"映射"}),
              el("div", {class:"blockTitleText", text:"fields"}),
              el("div", {class:"blockTitleSub", text:"系统字段名 -> 多维表格列名"}),
            ]),
          ]),
          el("div", {class:"form"}, [
            fields,
            el("div", {class:"help", text:"字段映射：key 为系统字段名，value 为多维表格列名。KEY 现在支持下拉选择，也可以手填。常用项：status（任务状态）、output（生成结果）、error（错误信息）、prompt_id（prompt_id/任务ID）、created_time（创建时间）、trigger_cmd（触发指令）、trigger_user（触发人，可选）。"}),
          ]),
        ]),
        el("div", {class:"block"}, [
          el("div", {class:"blockTitle"}, [
            el("div", {class:"blockTitleLeft"}, [
              el("span", {class:"pill", text:"状态"}),
              el("div", {class:"blockTitleText", text:"status_values"}),
              el("div", {class:"blockTitleSub", text:"内部状态 -> 表格显示值"}),
            ]),
          ]),
          el("div", {class:"form"}, [
            statusValues,
            el("div", {class:"help", text:"状态值映射：内部状态 -> 表格显示值。KEY 现在支持下拉选择，也可以手填。常用项：queued（待处理）、trigger（触发执行）、running（执行中）、done（已完成）、partial（部分完成）、failed（生成失败）。"}),
          ]),
        ]),
      ]));

      CURRENT = {
        commit: () => {
          const newKey = (keyInput.value || "").trim();
          if (!newKey) throw new Error("table key 不能为空");
          const out = deepCopy(orig);
          out.app_token = (appToken.value || "").trim();
          out.table_id = (tableId.value || "").trim();
          out.view_id = (viewId.value || "").trim();
          out.fields = fields._getValue();
          out.status_values = statusValues._getValue();
          if (newKey !== key) {
            delete tables[key];
          }
          tables[newKey] = out;
          STATE.selected = {type:"table", key:newKey};
        }
      };
      return;
    }

    if (t === "workflow") {
      const wfs = STATE.cfg.workflows || {};
      const orig = wfs[key] || {};
      const keyInput = el("input", {type:"text", value: key || "", placeholder:"workflow key"});
      const delBtn = el("button", {type:"button", text:"删除 workflow", class:"btnDanger"});

      const wfName = el("input", {type:"text", value: String(orig.workflowName || orig.workflow_name || "")});
      const provider = el("input", {type:"text", value: String(orig.provider || "")});
      const apiPath = el("input", {type:"text", value: String(orig.apiWorkflowPath || orig.api_workflow_path || "")});
      const baseUrl = el("input", {type:"text", value: String(orig.comfyuiBaseUrl || orig.comfyui_base_url || "")});
      const tableKey = el("input", {type:"text", value: String(orig.table || "")});
      const runLogTableKey = el("input", {type:"text", value: String(orig.runLogTable || orig.run_log_table || orig.runLogTableKey || orig.run_log_table_key || "")});
      const defaults = kvEditor(orig.defaults || {}, "key", "value");
      const recordFields = kvEditor(orig.recordFields || orig.record_fields || {}, "参数名(如 prompt)", "列名(如 提示词)");
      const writeBackFields = kvEditor(orig.writeBackFields || orig.write_back_fields || {}, "字段key(如 output)", "列名(如 生成结果)");
      const params = paramsEditor(orig.params || {});
      const strictParamValidation = el("input", {type:"checkbox", class:"switch"});
      strictParamValidation.checked = false;
      const runninghubWorkflowId = el("input", {type:"text", value: (orig.runninghub && orig.runninghub.workflowId) ? String(orig.runninghub.workflowId) : ""});

      const relOrig = orig.relationPrompt || orig.relation_prompt || {};
      const relationEnabled = el("input", {type:"checkbox", class:"switch"});
      relationEnabled.checked = !!(relOrig && typeof relOrig === "object" && Object.keys(relOrig).length);
      const relSourceField = el("input", {type:"text", value: String(relOrig.sourceField || relOrig.source_field || "")});
      const relTargetParam = el("input", {type:"text", value: String(relOrig.targetParam || relOrig.target_param || "prompt")});
      const relSplit = el("input", {type:"checkbox", class:"switch"});
      relSplit.checked = (relOrig.split == null) ? true : !!relOrig.split;
      const relStrict = el("input", {type:"checkbox", class:"switch"});
      relStrict.checked = (relOrig.strict == null) ? true : !!relOrig.strict;
      const relEnableItemParamMap = el("input", {type:"checkbox", class:"switch"});
      relEnableItemParamMap.checked = (relOrig.enableItemParamMap == null && relOrig.enable_item_param_map == null) ? true : !!(relOrig.enableItemParamMap ?? relOrig.enable_item_param_map);
      const relEnablePromptFields = el("input", {type:"checkbox", class:"switch"});
      relEnablePromptFields.checked = (relOrig.enablePromptFields == null && relOrig.enable_prompt_fields == null) ? true : !!(relOrig.enablePromptFields ?? relOrig.enable_prompt_fields);
      const relMaxItems = el("input", {type:"text", value: String(relOrig.maxItems || relOrig.max_items || "20")});
      const relTargetTableKey = el("input", {type:"text", value: String(relOrig.targetTableKey || relOrig.target_table_key || "")});
      const relTargetAppToken = el("input", {type:"text", value: String(relOrig.targetAppToken || relOrig.target_app_token || relOrig.app_token || "")});
      const relTargetTableId = el("input", {type:"text", value: String(relOrig.targetTableId || relOrig.target_table_id || relOrig.table_id || "")});
      const relTargetMatchField = el("input", {type:"text", value: String(relOrig.targetMatchField || relOrig.target_match_field || "")});
      const relIpm = relOrig.itemParamMap || relOrig.item_param_map || relOrig.item_params || {};
      const relItemParamMap = kvEditor(relIpm, "param(如 prompt_general)", "字段名(如 通用总控提示词)");
      const relPf = relOrig.promptFields || relOrig.prompt_fields || [];
      const relPromptFields = listEditor(relPf, "字段名(如 通用总控提示词)");
      const relJoinWith = el("input", {type:"text", value: String(relOrig.joinWith || relOrig.join_with || "\\n")});
      const workflowTableBindingEnabled = el("input", {type:"checkbox", class:"switch"});
      workflowTableBindingEnabled.checked = !!(
        String(orig.table || "").trim() ||
        String(orig.runLogTable || orig.run_log_table || orig.runLogTableKey || orig.run_log_table_key || "").trim() ||
        Object.keys(orig.recordFields || orig.record_fields || {}).length ||
        Object.keys(orig.writeBackFields || orig.write_back_fields || {}).length ||
        (relOrig && typeof relOrig === "object" && Object.keys(relOrig).length)
      );

      function applyRelationEnabled() {
        const show = workflowTableBindingEnabled.checked && relationEnabled.checked;
        for (const it of Array.from(root.querySelectorAll(".relationOnly"))) it.style.display = show ? "" : "none";
        const showItem = show && relEnableItemParamMap.checked;
        const showPrompt = show && relEnablePromptFields.checked;
        for (const it of Array.from(root.querySelectorAll(".relItemBody"))) it.style.display = showItem ? "" : "none";
        for (const it of Array.from(root.querySelectorAll(".relPromptBody"))) it.style.display = showPrompt ? "" : "none";
      }
      function applyWorkflowTableBindingEnabled() {
        const show = bitableEnabled && workflowTableBindingEnabled.checked;
        for (const it of Array.from(root.querySelectorAll(".workflowTableOnly"))) it.style.display = show ? "" : "none";
        applyRelationEnabled();
      }
      relationEnabled.onchange = () => applyRelationEnabled();
      relEnableItemParamMap.onchange = () => applyRelationEnabled();
      relEnablePromptFields.onchange = () => applyRelationEnabled();
      workflowTableBindingEnabled.onchange = () => applyWorkflowTableBindingEnabled();

      delBtn.onclick = () => {
        delete wfs[key];
        STATE.selected = {type:"env", key: Object.keys(STATE.env||{}).sort()[0] || ""};
        renderSidebar();
        renderEditor();
      };

      const mode = String(STATE.envMeta.bitable_mode || "").trim().toLowerCase();
      const bitableEnabled = mode !== "off";

      root.appendChild(el("div", {class:"sectionHeader"}, [
        el("div", null, [el("h2", {text:"workflow: " + (key || "")}), el("div", {class:"help", text:"配置工作流基础信息、参数映射与回写字段。"})]),
        delBtn,
      ]));
      root.appendChild(el("div", {class:"form"}, [
        el("div", {class:"field"}, [el("div", {class:"label", text:"workflow key"}), keyInput, el("div", {class:"help", text:"用于命令/默认工作流选择。"})]),
        el("div", {class:"field"}, [el("div", {class:"label", text:"workflowName"}), wfName, el("div", {class:"help", text:"展示给用户看的名字。"})]),
        el("div", {class:"row2"}, [
          el("div", {class:"field"}, [el("div", {class:"label", text:"provider"}), provider, el("div", {class:"help", text:"留空表示本地 ComfyUI；runninghub 表示走 RunningHub。"})]),
          el("div", {class:"field"}, [el("div", {class:"label", text:"runninghub.workflowId"}), runninghubWorkflowId, el("div", {class:"help", text:"provider=runninghub 时使用。"})]),
        ]),
        el("div", {class:"row2"}, [
          el("div", {class:"field"}, [el("div", {class:"label", text:"apiWorkflowPath"}), apiPath, el("div", {class:"help", text:"降级兜底用：当 /prompt_workflow 返回 404（插件缺失/工作流不存在）时，读取该 JSON（需为 ComfyUI API Format）并改走 /prompt 执行。"})]),
          el("div", {class:"field"}, [el("div", {class:"label", text:"comfyuiBaseUrl（可选）"}), baseUrl, el("div", {class:"help", text:"不填则用 .env 的 COMFYUI_BASE_URL。"})]),
        ]),
        el("div", {class:"field bitableOnly"}, [
          el("div", {class:"label", text:"关联表格"}),
          workflowTableBindingEnabled,
          el("div", {class:"help", text:"总开关。关闭时会隐藏并在保存时清空 table、runLogTable、recordFields、writeBackFields、relationPrompt 这些表格相关配置。"}),
        ]),
        el("div", {class:"field bitableOnly workflowTableOnly"}, [el("div", {class:"label", text:"table"}), tableKey, el("div", {class:"help", text:"绑定的表格 key（对应 tables）。"} )]),
        el("div", {class:"field bitableOnly workflowTableOnly"}, [el("div", {class:"label", text:"runLogTable（运行记录表）"}), runLogTableKey, el("div", {class:"help", text:"把每个子任务的提交/成功/失败/结果写到这张表。填 tables 里的 key（例如 runlog_table）。留空表示不记录。"} )]),
        el("div", {class:"block"}, [
          el("div", {class:"blockTitle"}, [
            el("div", {class:"blockTitleLeft"}, [
              el("span", {class:"pill", text:"默认"}),
              el("div", {class:"blockTitleText", text:"defaults"}),
              el("div", {class:"blockTitleSub", text:"兜底参数：表格/命令没给时用；命令参数会覆盖它"}),
            ]),
          ]),
          el("div", {class:"form"}, [
            defaults,
            el("div", {class:"help", text:"常见用途：save_prefix_1/save_prefix_2（若绑定了 record 且用户没传，会自动拼上 record_id 以避免重名）。"}),
          ]),
        ]),
        el("div", {class:"block bitableOnly workflowTableOnly"}, [
          el("div", {class:"blockTitle"}, [
            el("div", {class:"blockTitleLeft"}, [
              el("span", {class:"pill", text:"读取"}),
              el("div", {class:"blockTitleText", text:"recordFields"}),
              el("div", {class:"blockTitleSub", text:"从表A读取：参数名 -> 列名"}),
            ]),
          ]),
          el("div", {class:"form"}, [
            recordFields,
            el("div", {class:"help", text:"例：prompt -> 提示词；images -> 产品图。"}),
          ]),
        ]),
        el("div", {class:"block bitableOnly workflowTableOnly"}, [
          el("div", {class:"blockTitle"}, [
            el("div", {class:"blockTitleLeft"}, [
              el("span", {class:"pill", text:"回写"}),
              el("div", {class:"blockTitleText", text:"writeBackFields"}),
              el("div", {class:"blockTitleSub", text:"写回表A：字段key -> 列名"}),
            ]),
          ]),
          el("div", {class:"form"}, [
            writeBackFields,
            el("div", {class:"help", text:"例：output -> 结果图；prompt_id -> 任务ID；status -> 任务状态。"}),
          ]),
        ]),
        el("div", {class:"block bitableOnly workflowTableOnly"}, [
          el("div", {class:"blockTitle"}, [
            el("div", {class:"blockTitleLeft"}, [
              el("span", {class:"pill", text:"关联"}),
              el("div", {class:"blockTitleText", text:"relationPrompt（方案B）"}),
              el("div", {class:"blockTitleSub", text:"用表A的选择值/record_id 去表B查提示词，再拼接成最终 prompt"}),
            ]),
          ]),
          el("div", {class:"form"}, [
            el("div", {class:"field"}, [
              el("div", {class:"label", text:"启用 relationPrompt"}),
              relationEnabled,
              el("div", {class:"help", text:"开启后会走跨表查询；关闭则回到 recordFields 的普通读取逻辑。"}),
            ]),
            el("div", {class:"row2 relationOnly"}, [
              el("div", {class:"field"}, [el("div", {class:"label", text:"sourceField"}), relSourceField, el("div", {class:"help", text:"表A字段名（例如：选择屏数）。优先使用里面的 record_ids；没有 record_ids 才会用文字匹配。"})]),
              el("div", {class:"field"}, [el("div", {class:"label", text:"targetParam"}), relTargetParam, el("div", {class:"help", text:"把拼接好的提示词写到哪个参数里（通常是 prompt）。"})]),
            ]),
            el("div", {class:"row2 relationOnly"}, [
              el("div", {class:"field"}, [el("div", {class:"label", text:"split"}), relSplit, el("div", {class:"help", text:"勾选：关联到几条就提交几次。"})]),
              el("div", {class:"field"}, [el("div", {class:"label", text:"strict"}), relStrict, el("div", {class:"help", text:"勾选：匹配不到表B记录时直接报错，避免悄悄用空提示词提交。"})]),
            ]),
            el("div", {class:"field relationOnly"}, [el("div", {class:"label", text:"maxItems"}), relMaxItems, el("div", {class:"help", text:"最多处理多少条关联（默认 20）。"})]),
            el("div", {class:"row2 relationOnly"}, [
              el("div", {class:"field"}, [el("div", {class:"label", text:"targetTableKey（推荐）"}), relTargetTableKey, el("div", {class:"help", text:"目标表在 tables 里的 key。填了它就不必填 app_token/table_id。"})]),
              el("div", {class:"field"}, [el("div", {class:"label", text:"targetMatchField（可选）"}), relTargetMatchField, el("div", {class:"help", text:"当 sourceField 只有文字没有 record_ids 时，用该列做匹配（例如：类型）。"})]),
            ]),
            el("div", {class:"row2 relationOnly"}, [
              el("div", {class:"field"}, [el("div", {class:"label", text:"targetAppToken（可选）"}), relTargetAppToken, el("div", {class:"help", text:"不使用 targetTableKey 时需要填（app_token）。"})]),
              el("div", {class:"field"}, [el("div", {class:"label", text:"targetTableId（可选）"}), relTargetTableId, el("div", {class:"help", text:"不使用 targetTableKey 时需要填（table_id）。"})]),
            ]),
            el("div", {class:"subBlock relationOnly"}, [
              el("div", {class:"subHeader"}, [el("div", {class:"subTitle", text:"itemParamMap（不拼接）"}), relEnableItemParamMap]),
              el("div", {class:"relBody relItemBody"}, [
                el("div", {class:"help", text:"每条关联记录会生成一组参数包。例：prompt_general -> 通用总控提示词。注意：这些 param 需要在下方 params 里配置 targets 才会真正写进 ComfyUI 节点。"}),
                relItemParamMap,
              ]),
            ]),
            el("div", {class:"subBlock relationOnly"}, [
              el("div", {class:"subHeader"}, [el("div", {class:"subTitle", text:"promptFields（拼接）"}), relEnablePromptFields]),
              el("div", {class:"relBody relPromptBody"}, [
                el("div", {class:"help", text:"会额外生成一段“合成提示词”：按顺序取表B字段，再用 joinWith 拼接，写入 targetParam（通常是 prompt）。如果同时启用 itemParamMap，就等于：既保留独立字段，又生成合成字段。"}),
                el("div", {class:"field"}, [el("div", {class:"label", text:"joinWith"}), relJoinWith, el("div", {class:"help", text:"拼接多个字段的连接符。填 \\n 表示换行。"})]),
                relPromptFields,
              ]),
            ]),
          ]),
        ]),
        (() => {
          const paramsBlock = el("div", {class:"block paramsBlock"}, [
          el("div", {class:"blockTitle"}, [
            el("div", {class:"blockTitleLeft"}, [
              el("span", {class:"pill", text:"映射"}),
              el("div", {class:"blockTitleText", text:"params"}),
              el("div", {class:"blockTitleSub", text:"把参数写进 ComfyUI 工作流节点（nodeId/fieldName/index）"}),
            ]),
          ]),
          el("div", {class:"form"}, [
            el("div", {class:"field"}, [
              el("div", {class:"label", text:"校验空参数名"}),
              strictParamValidation,
              el("div", {class:"help", text:"默认关闭：空白参数卡不会拦住切页或保存。打开后：只要 params 里有填了内容但参数名为空，就提示报错。"}),
            ]),
            params,
          ]),
          ]);
          root._paramsBlock = paramsBlock;
          return paramsBlock;
        })(),
      ]));

      function applyStrictParamValidationTone() {
        const pb = root._paramsBlock;
        if (pb) {
          if (strictParamValidation.checked) pb.classList.add("paramsStrictOn");
          else pb.classList.remove("paramsStrictOn");
        }
        if (params && params._setStrictTone) params._setStrictTone(!!strictParamValidation.checked);
      }
      strictParamValidation.onchange = () => applyStrictParamValidationTone();

      const items = root.querySelectorAll(".bitableOnly");
      for (const it of items) it.style.display = bitableEnabled ? "" : "none";

      CURRENT = {
        commit: () => {
          const newKey = (keyInput.value || "").trim();
          if (!newKey) throw new Error("workflow key 不能为空");
          const out = deepCopy(orig);
          const nameV = (wfName.value || "").trim();
          if (nameV) out.workflowName = nameV; else delete out.workflowName;
          const providerV = (provider.value || "").trim();
          if (providerV) out.provider = providerV; else delete out.provider;
          const apiPathV = (apiPath.value || "").trim();
          if (apiPathV) out.apiWorkflowPath = apiPathV; else delete out.apiWorkflowPath;
          const baseUrlV = (baseUrl.value || "").trim();
          if (baseUrlV) out.comfyuiBaseUrl = baseUrlV; else delete out.comfyuiBaseUrl;
          out.defaults = defaults._getValue();
          if (workflowTableBindingEnabled.checked) {
            const tableV = (tableKey.value || "").trim();
            if (tableV) out.table = tableV; else delete out.table;
            const rlt = (runLogTableKey.value || "").trim();
            if (rlt) out.runLogTable = rlt; else delete out.runLogTable;
            out.recordFields = recordFields._getValue();
            out.writeBackFields = writeBackFields._getValue();

            if (relationEnabled.checked) {
              const rp = deepCopy(relOrig && typeof relOrig === "object" ? relOrig : {});
              const sf = (relSourceField.value || "").trim();
              if (!sf) throw new Error("relationPrompt 启用时，sourceField 不能为空");
              rp.sourceField = sf;
              const tp = (relTargetParam.value || "").trim();
              rp.targetParam = tp || "prompt";
              rp.split = !!relSplit.checked;
              rp.strict = !!relStrict.checked;
              rp.enableItemParamMap = !!relEnableItemParamMap.checked;
              rp.enablePromptFields = !!relEnablePromptFields.checked;
              const mi = (relMaxItems.value || "").trim();
              if (mi) {
                const n = parseInt(mi, 10);
                if (!isNaN(n)) rp.maxItems = n;
              } else {
                delete rp.maxItems;
              }
              const jw = (relJoinWith.value || "").trim();
              rp.joinWith = jw ? jw.replace(/\\n/g, "\n") : "\n";
              const ttk = (relTargetTableKey.value || "").trim();
              if (ttk) rp.targetTableKey = ttk; else delete rp.targetTableKey;
              const tam = (relTargetAppToken.value || "").trim();
              if (tam) rp.targetAppToken = tam; else delete rp.targetAppToken;
              const tti = (relTargetTableId.value || "").trim();
              if (tti) rp.targetTableId = tti; else delete rp.targetTableId;
              const tmf = (relTargetMatchField.value || "").trim();
              if (tmf) rp.targetMatchField = tmf; else delete rp.targetMatchField;

              const ipm = rp.enableItemParamMap ? relItemParamMap._getValue() : {};
              const ipmKeys = Object.keys(ipm || {});
              if (ipmKeys.length) rp.itemParamMap = ipm; else delete rp.itemParamMap;

              const pf = rp.enablePromptFields ? relPromptFields._getValue() : [];
              if (!pf.length && !ipmKeys.length) throw new Error("relationPrompt 启用时，itemParamMap 或 promptFields 至少启用一个并填写内容");
              if (pf.length) rp.promptFields = pf; else delete rp.promptFields;
              out.relationPrompt = rp;
            } else {
              delete out.relationPrompt;
            }
          } else {
            delete out.table;
            delete out.runLogTable;
            delete out.recordFields;
            delete out.writeBackFields;
            delete out.relationPrompt;
          }
          out.params = params._getValue({strictEmptyName: strictParamValidation.checked});

          const rhId = (runninghubWorkflowId.value || "").trim();
          if (rhId) out.runninghub = {...(out.runninghub||{}), workflowId: rhId};
          else if (out.runninghub && Object.keys(out.runninghub).length === 1 && out.runninghub.workflowId) delete out.runninghub;
          else if (out.runninghub) delete out.runninghub.workflowId;

          if (newKey !== key) delete wfs[key];
          wfs[newKey] = out;
          STATE.selected = {type:"workflow", key:newKey};
        }
      };
      applyStrictParamValidationTone();
      applyWorkflowTableBindingEnabled();
      applyRelationEnabled();
      return;
    }
  }

  async function loadAll() {
    setStatus("加载中...", "muted");
    const r1 = await fetch("/admin/api/env", {headers: authHeaders()});
    if (!r1.ok) {
      const hint = (r1.status === 401 || r1.status === 403) ? "（请在上方输入 ADMIN_TOKEN，或 URL 加 ?token=... ）" : "";
      setStatus("读取 .env 失败: " + r1.status + hint, "err");
      if (r1.status === 401 || r1.status === 403) $("token").focus();
      return;
    }
    const env = await r1.json();
    STATE.env = env.values || {};
    STATE.envMeta = env.meta || {};

    const r2 = await fetch("/admin/api/workflows", {headers: authHeaders()});
    if (!r2.ok) {
      const hint = (r2.status === 401 || r2.status === 403) ? "（请在上方输入 ADMIN_TOKEN，或 URL 加 ?token=... ）" : "";
      setStatus("读取 workflows 失败: " + r2.status + hint, "err");
      if (r2.status === 401 || r2.status === 403) $("token").focus();
      return;
    }
    const wf = await r2.json();
    STATE.cfg = wf.config || {};
    $("wfPath").textContent = "path: " + String(wf.path || "");
    if (!STATE.cfg.tables) STATE.cfg.tables = {};
    if (!STATE.cfg.workflows) STATE.cfg.workflows = {};
    if (!STATE.cfg.automation || typeof STATE.cfg.automation !== "object") STATE.cfg.automation = {};

    const firstEnv = Object.keys(STATE.env || {}).sort()[0] || "";
    if (!STATE.selected.key) STATE.selected = {type:"env", key:firstEnv};

    renderSidebar();
    renderEditor();

    if (wf.admin_token_missing) setStatus("ADMIN_TOKEN 未设置：仅允许本机访问", "warn");
    else setStatus("已加载", "ok");
  }

  async function saveAll() {
    try {
      setStatus("保存中...", "muted");
      if (!commitCurrent()) return;
      renderSidebar();
      renderEditor();

      const envUpdates = deepCopy(STATE.env || {});
      const wfCfg = deepCopy(STATE.cfg || {});

      const r1 = await fetch("/admin/api/env?reload=0", {
        method: "PUT",
        headers: {"content-type":"application/json", ...authHeaders()},
        body: JSON.stringify({values: envUpdates}),
      });
      if (!r1.ok) {
        const t = await r1.text();
        setStatus("保存 .env 失败: " + t, "err");
        return;
      }

      const r2 = await fetch("/admin/api/workflows?reload=0", {
        method: "PUT",
        headers: {"content-type":"application/json", ...authHeaders()},
        body: JSON.stringify({config: wfCfg}),
      });
      if (!r2.ok) {
        const t = await r2.text();
        setStatus("保存 workflows 失败: " + t, "err");
        return;
      }

      const r3 = await fetch("/admin/api/reload", {method:"POST", headers: authHeaders()});
      if (!r3.ok) {
        const t = await r3.text();
        setStatus("重载失败: " + t, "err");
        return;
      }

      setStatus("已保存并重载", "ok");
      await loadAll();
    } catch (e) {
      setStatus(String(e && e.message ? e.message : e), "err");
    }
  }

  $("btnReloadAll").onclick = () => loadAll();
  $("btnSaveAll").onclick = () => saveAll();
  $("btnAddTable").onclick = () => {
    const mode = String(STATE.envMeta.bitable_mode || "").trim().toLowerCase();
    if (mode === "off") return;
    if (!commitCurrent()) return;
    const key = "new_table";
    const tables = STATE.cfg.tables || {};
    if (!tables[key]) tables[key] = {app_token:"", table_id:"", view_id:"", fields:{}, status_values:{}};
    select("table", key);
  };
  $("btnAddWorkflow").onclick = () => {
    if (!commitCurrent()) return;
    const key = "new_workflow";
    const wfs = STATE.cfg.workflows || {};
    if (!wfs[key]) wfs[key] = {workflowName:"", params:{}};
    select("workflow", key);
  };

  const url = new URL(location.href);
  const q = url.searchParams.get("token");
  if (q) $("token").value = q;
  else {
    try {
      const saved = String(localStorage.getItem("admin_token") || "");
      if (saved.trim()) $("token").value = saved.trim();
    } catch (e) {}
  }
  $("token").addEventListener("change", () => {
    const v = String($("token").value || "").trim();
    try {
      if (v) localStorage.setItem("admin_token", v);
    } catch (e) {}
  });
  loadAll();
</script>
</body>
</html>
"""


def register_admin(app: FastAPI, ctx: AppContext) -> None:
    @app.get("/admin/config", dependencies=[Depends(_require_admin)])
    async def admin_config_page() -> HTMLResponse:
        return HTMLResponse(
            _PAGE_HTML,
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
            },
        )

    @app.get("/admin/api/env", dependencies=[Depends(_require_admin)])
    async def admin_get_env() -> dict[str, Any]:
        path = _env_path()
        with _LOCK:
            _, values = _read_env_file(path)
        visible = {k: v for k, v in values.items() if not _is_sensitive_key(k) and not _is_hidden_env_key(k)}
        bitable_mode = str(values.get("BITABLE_MODE") or "").strip().lower()
        return {"values": visible, "path": str(path), "meta": {"bitable_mode": bitable_mode}}

    @app.put("/admin/api/env", dependencies=[Depends(_require_admin)])
    async def admin_put_env(payload: dict[str, Any] = Body(default_factory=dict), reload: int = 0) -> dict[str, Any]:
        raw = payload.get("values") or {}
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail="invalid values")
        updates: dict[str, str] = {}
        for k, v in raw.items():
            if not isinstance(k, str) or not k.strip():
                continue
            kk = k.strip()
            if _is_sensitive_key(kk) or _is_hidden_env_key(kk):
                continue
            updates[kk] = "" if v is None else str(v)
        path = _env_path()
        with _LOCK:
            _write_env_file(path, updates)
        if reload:
            _reload_context_inplace(ctx)
        return {"ok": True}

    @app.get("/admin/api/workflows", dependencies=[Depends(_require_admin)])
    async def admin_get_workflows() -> dict[str, Any]:
        path = _workflow_path()
        with _LOCK:
            cfg = _read_workflow_config(path)
        return {"config": cfg, "path": str(path), "admin_token_missing": not bool((os.environ.get("ADMIN_TOKEN") or "").strip())}

    @app.put("/admin/api/workflows", dependencies=[Depends(_require_admin)])
    async def admin_put_workflows(payload: dict[str, Any] = Body(default_factory=dict), reload: int = 0) -> dict[str, Any]:
        cfg = payload.get("config")
        if not isinstance(cfg, dict):
            raise HTTPException(status_code=400, detail="invalid config")
        try:
            json.dumps(cfg)
        except Exception:
            raise HTTPException(status_code=400, detail="config not serializable")
        path = _workflow_path()
        with _LOCK:
            _write_workflow_config(path, cfg)
        if reload:
            _reload_context_inplace(ctx)
        return {"ok": True}

    @app.post("/admin/api/reload", dependencies=[Depends(_require_admin)])
    async def admin_reload() -> dict[str, Any]:
        _reload_context_inplace(ctx)
        return {"ok": True}
