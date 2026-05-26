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
    return root / "config" / "workflows.loca.json"


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
    .row2 { display:grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    @media (max-width: 980px) { .row2 { grid-template-columns: 1fr; } }
    table { border-collapse: separate; border-spacing: 0; width: 100%; max-width: 100%; overflow: hidden; border-radius: 12px; table-layout: fixed; }
    th, td { padding: 10px 10px; vertical-align: top; border-bottom: 1px solid rgba(255,255,255,0.08); }
    th { text-align: left; color: rgba(229,231,235,0.9); background: rgba(255,255,255,0.06); font-weight: 600; }
    td { color: rgba(229,231,235,0.92); overflow-wrap: anywhere; word-break: break-word; }
    .kvWrap { display:grid; gap: 8px; }
    .kvActions { display:flex; gap: 10px; }
    .bitableOnly { }
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

        <div class="sideTitle bitableOnly" id="tablesTitle" style="margin-top:14px">
          <h2>tables</h2>
          <button id="btnAddTable" class="btnGhost">新增</button>
        </div>
        <div id="tableList" class="sideList bitableOnly"></div>

        <div class="sideTitle" style="margin-top:14px">
          <h2>workflows</h2>
          <button id="btnAddWorkflow" class="btnGhost">新增</button>
        </div>
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
  let CURRENT = null;

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

  function setStatus(text, cls) {
    const el0 = $("status");
    el0.className = cls || "muted";
    el0.textContent = text || "";
  }

  function authHeaders() {
    const url = new URL(location.href);
    const q = url.searchParams.get("token");
    const t = ($("token").value || q || "").trim();
    return t ? {"x-admin-token": t} : {};
  }

  function deepCopy(v) {
    return JSON.parse(JSON.stringify(v || {}));
  }

  function kvEditor(initial, keyPlaceholder, valuePlaceholder) {
    const wrap = el("div", {class:"kvWrap"});
    const table = el("table");
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

    const addBtn = el("button", {type:"button", text:"新增一行", class:"btnGhost"});
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
      const card = el("div", {class:"card", style:"padding:12px"});
      const head = el("div", {class:"sectionHeader"}, [
        el("span", {class:"pill", text:"param"}),
        el("input", {type:"text", value: key || "", placeholder:"参数名(如 prompt)"}),
        el("button", {type:"button", text:"删除", class:"btnDanger"})
      ]);
      const nameInput = head.querySelector("input");
      const delBtn = head.querySelector("button");
      delBtn.onclick = () => card.remove();

      const typeInput = el("input", {type:"text", value: (orig.type||""), placeholder:"type(如 str)"});
      const targets = targetsEditor(orig.targets || []);
      const form = el("div", {class:"form"}, [
        el("div", {class:"field"}, [el("div", {class:"label", text:"type"}), typeInput]),
        el("div", {class:"field"}, [el("div", {class:"label", text:"targets"}), targets, el("div", {class:"help", text:"targets 用于把参数写入工作流节点：nodeId 为节点编号，fieldName 为字段名，index 仅用于数组位置（可空）。"})]),
      ]);
      card.appendChild(head);
      card.appendChild(form);
      card._collect = () => {
        const name = (nameInput.value || "").trim();
        if (!name) throw new Error("params 里有参数名为空");
        const out = {...orig};
        const t = (typeInput.value || "").trim();
        if (t) out.type = t; else delete out.type;
        out.targets = targets._getValue();
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
    wrap._getValue = () => {
      const out = {};
      for (const card of Array.from(list.children)) {
        const [k, v] = card._collect();
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
      const fields = kvEditor(orig.fields || {}, "字段key(如 status)", "列名(如 任务状态)");
      const statusValues = kvEditor(orig.status_values || {}, "状态key(如 queued)", "显示值(如 待处理)");

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
        el("div", {class:"field"}, [el("div", {class:"label", text:"table key"}), keyInput, el("div", {class:"help", text:"用于在 workflows 中引用该表格。"})]),
        el("div", {class:"row2"}, [
          el("div", {class:"field"}, [el("div", {class:"label", text:"app_token"}), appToken, el("div", {class:"help", text:"飞书多维表格 app_token。"})]),
          el("div", {class:"field"}, [el("div", {class:"label", text:"table_id"}), tableId, el("div", {class:"help", text:"飞书多维表格 table_id（表格 ID）。"})]),
        ]),
        el("div", {class:"field"}, [el("div", {class:"label", text:"view_id（可选）"}), viewId, el("div", {class:"help", text:"不填表示默认视图。"})]),
        el("div", {class:"field"}, [el("div", {class:"label", text:"fields"}), fields, el("div", {class:"help", text:"字段映射：key 为系统字段名，value 为多维表格列名。"})]),
        el("div", {class:"field"}, [el("div", {class:"label", text:"status_values"}), statusValues, el("div", {class:"help", text:"状态值映射：queued/running/done/failed 等内部状态 -> 表格显示值。"})]),
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
      const defaults = kvEditor(orig.defaults || {}, "key", "value");
      const recordFields = kvEditor(orig.recordFields || orig.record_fields || {}, "参数名(如 prompt)", "列名(如 提示词)");
      const writeBackFields = kvEditor(orig.writeBackFields || orig.write_back_fields || {}, "字段key(如 output)", "列名(如 生成结果)");
      const params = paramsEditor(orig.params || {});
      const runninghubWorkflowId = el("input", {type:"text", value: (orig.runninghub && orig.runninghub.workflowId) ? String(orig.runninghub.workflowId) : ""});

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
          el("div", {class:"field"}, [el("div", {class:"label", text:"apiWorkflowPath"}), apiPath, el("div", {class:"help", text:"本地工作流 JSON 路径（本地执行时使用）。"})]),
          el("div", {class:"field"}, [el("div", {class:"label", text:"comfyuiBaseUrl（可选）"}), baseUrl, el("div", {class:"help", text:"不填则用 .env 的 COMFYUI_BASE_URL。"})]),
        ]),
        el("div", {class:"field bitableOnly"}, [el("div", {class:"label", text:"table"}), tableKey, el("div", {class:"help", text:"绑定的表格 key（对应 tables）。"} )]),
        el("div", {class:"field"}, [el("div", {class:"label", text:"defaults"}), defaults, el("div", {class:"help", text:"默认参数：当用户没传值时使用。"})]),
        el("div", {class:"field bitableOnly"}, [el("div", {class:"label", text:"recordFields"}), recordFields, el("div", {class:"help", text:"从表格读取：参数名 -> 表格列名。"})]),
        el("div", {class:"field bitableOnly"}, [el("div", {class:"label", text:"writeBackFields"}), writeBackFields, el("div", {class:"help", text:"写回表格：字段 key -> 表格列名。"})]),
        el("div", {class:"field"}, [el("div", {class:"label", text:"params"}), params]),
      ]));

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
          const tableV = (tableKey.value || "").trim();
          if (tableV) out.table = tableV; else delete out.table;
          out.defaults = defaults._getValue();
          out.recordFields = recordFields._getValue();
          out.writeBackFields = writeBackFields._getValue();
          out.params = params._getValue();

          const rhId = (runninghubWorkflowId.value || "").trim();
          if (rhId) out.runninghub = {...(out.runninghub||{}), workflowId: rhId};
          else if (out.runninghub && Object.keys(out.runninghub).length === 1 && out.runninghub.workflowId) delete out.runninghub;
          else if (out.runninghub) delete out.runninghub.workflowId;

          if (newKey !== key) delete wfs[key];
          wfs[newKey] = out;
          STATE.selected = {type:"workflow", key:newKey};
        }
      };
      return;
    }
  }

  async function loadAll() {
    setStatus("加载中...", "muted");
    const r1 = await fetch("/admin/api/env", {headers: authHeaders()});
    if (!r1.ok) { setStatus("读取 .env 失败: " + r1.status, "err"); return; }
    const env = await r1.json();
    STATE.env = env.values || {};
    STATE.envMeta = env.meta || {};

    const r2 = await fetch("/admin/api/workflows", {headers: authHeaders()});
    if (!r2.ok) { setStatus("读取 workflows 失败: " + r2.status, "err"); return; }
    const wf = await r2.json();
    STATE.cfg = wf.config || {};
    if (!STATE.cfg.tables) STATE.cfg.tables = {};
    if (!STATE.cfg.workflows) STATE.cfg.workflows = {};

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
  if (url.searchParams.get("token")) $("token").value = url.searchParams.get("token");
  loadAll();
</script>
</body>
</html>
"""


def register_admin(app: FastAPI, ctx: AppContext) -> None:
    @app.get("/admin/config", dependencies=[Depends(_require_admin)])
    async def admin_config_page() -> HTMLResponse:
        return HTMLResponse(_PAGE_HTML)

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
