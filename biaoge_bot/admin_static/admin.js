const $ = (id) => document.getElementById(id);
const ENV_DESC = {
  "BOT_LOG_LEVEL": "日志等级（INFO/DEBUG/WARNING/ERROR）。排查问题时可临时改为 DEBUG。",
  "CALLBACK_DUMP_ENABLED": "是否保存回调 payload 调试 dump（1/0）。开启后统一写入 logs/dumps/callbacks。",
  "SAVE_TASK_REQUEST_PARAMS": "是否保存任务请求参数调试 dump（1/0）。开启后统一写入 logs/dumps/task_requests。",
  "FEISHU_SEND_RESULT_TO_CHAT": "绑定表格的任务完成后，是否也把生成结果发送回触发的飞书对话框（1/0）。",
  "FEISHU_UPLOAD_RATE_LIMIT_RETRIES": "飞书附件/图片上传遇到限频时的重试次数，取值 1-10；默认 4。",
  "BIAOGE_CA_BUNDLE": "TLS 证书包路径。通常留空自动使用 certifi；若 macOS 飞书长连接报 CERTIFICATE_VERIFY_FAILED，且网络代理/安全软件使用自签根证书，可填包含该根证书的 PEM 文件。",
  "BITABLE_HTTP_TIMEOUT_SECONDS": "飞书多维表格接口读写超时时间（秒）。默认 10；网络慢或偶发 ReadTimeout 时可调大到 20-30。",
  "CALLBACK_HOST": "本机/局域网可访问的监听地址。示例：127.0.0.1（仅本机）或 192.168.x.x（局域网可访问）。",
  "CALLBACK_PORT": "回调服务端口，配置页也是通过该端口访问。",
  "COMFYUI_BASE_URL": "ComfyUI 服务地址（示例：http://127.0.0.1:8188 或远程地址）。",
  "COMFYUI_INPUT_DIR": "ComfyUI 输入目录（可选）。留空表示不指定。",
  "TEMP_DOWNLOAD_DIR": "表格附件下载的临时目录。支持相对路径、绝对路径、~ 和环境变量占位符；相对路径基于项目根目录。",
  "COMFYUI_UPLOAD_ENABLED": "是否允许上传图片到 ComfyUI（1/0）。",
  "COMFYUI_UPLOAD_TIMEOUT_SECONDS": "上传图片到 ComfyUI /upload/image 的超时时间（秒），默认 20。本地服务正常应很快返回；超时通常表示 ComfyUI 卡住、input 目录不可写，或需要关闭 COMFYUI_UPLOAD_ENABLED 改走本地输入目录。",
  "COMFYUI_UPLOAD_SUBFOLDER": "上传到 ComfyUI 的子目录（可选）。",
  "COMFYUI_UPLOAD_OVERWRITE": "上传同名文件时是否覆盖（true/false）。",
  "RESULT_OUTPUT_DIR": "结果输出目录（可选）。填绝对/相对路径时保存生成结果，不填则只做临时中转不落盘。示例：output 或 ${BIAOGE_ROOT}/output（推荐统一用正斜杠 /）。",
  "REMOTE_CALLBACK_URL": "公网/远程回调地址（可选）。适用于外部服务能回调到你指定的地址的情况。",
  "FEISHU_AT_USER_ID": "本地机器人的 bot_open_id，用于 FC 转发器发送 /cb 消息时 @ 本机器人。可在飞书里发送 /botid 获取；轮询模式可留空。",
  "REMOTE_RESULT_MODE": "远程结果获取模式（例如 poll/fc）。",
  "REMOTE_POLL_INTERVAL_SECONDS": "远程轮询间隔（秒），过小可能导致请求过频。",
  "REMOTE_POLL_FALLBACK_SECONDS": "远程轮询兜底超时（秒），超过将走兜底策略。",
};
const STATE = {
  env: {},
  envMeta: {},
  envSchema: {},
  envGroupOrder: [],
  cfg: {},
  selected: {type: "env", key: ""},
};
const PARAM_TYPE_OPTIONS = ["str", "int", "float", "bool"];
const TABLE_FIELD_KEY_OPTIONS = ["status", "output", "output_image", "output_video", "output_audio", "output_text", "error", "prompt_id", "created_time", "task_name"];
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
    const val = el("input", {type:"text", value: (v==null ? "" : (typeof v === "object" ? JSON.stringify(v) : String(v))), placeholder: valuePlaceholder || ""});
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
  wrap._setValue = (obj) => {
    while (tbody.firstChild) tbody.removeChild(tbody.firstChild);
    const o = obj && typeof obj === "object" ? obj : {};
    const keys = Object.keys(o);
    if (keys.length) {
      for (const k of keys) addRow(k, o[k]);
    } else {
      addRow("", "");
    }
  };
  return wrap;
}

function listEditor(initialList, valuePlaceholder) {
  const wrap = el("div", {class:"kvWrap"});
  const table = el("table");
  const tbody = el("tbody");
  table.appendChild(tbody);

  function addRow(v) {
    const val = el("input", {type:"text", value: (v==null ? "" : (typeof v === "object" ? JSON.stringify(v) : String(v))), placeholder: valuePlaceholder || ""});
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
  wrap._setValue = (list0) => {
    while (tbody.firstChild) tbody.removeChild(tbody.firstChild);
    const init = Array.isArray(list0) ? list0 : (typeof list0 === "string" ? [list0] : []);
    if (init.length) {
      for (const v of init) addRow(v);
    } else {
      addRow("");
    }
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

  function relationPromptsEditor(initialPrompts) {
    const wrap = el("div", {class:"kvWrap"});
    const list = el("div", {class:"kvWrap"});
    const helpEl = el("div", {class:"help", text:"配置多张子表，支持笛卡尔积split。"});

    function renumberCards() {
      const cards = Array.from(list.children);
      for (let i = 0; i < cards.length; i++) {
        const card = cards[i];
        if (card && card._pillEl) card._pillEl.textContent = "子表" + String(i + 1);
      }
    }

    function updateEmptyVis() {
      const empty = list.children.length === 0;
      helpEl.style.display = empty ? "none" : "";
      list.style.display = empty ? "none" : "";
      renumberCards();
    }

    function addPromptItem(promptSpec) {
      const orig = promptSpec && typeof promptSpec === "object" ? promptSpec : {};
      const card = el("div", {class:"card paramCard", style:"padding:12px"});
      const pillEl = el("span", {class:"pill", text:"子表"});
      const head = el("div", {class:"sectionHeader"}, [
        pillEl,
        el("div", {style:"flex:1"}),
        el("button", {type:"button", text:"删除", class:"btnDanger"})
      ]);
      const delBtn = head.querySelector("button");
      delBtn.onclick = () => {
        card.remove();
        updateEmptyVis();
      };
      card._pillEl = pillEl;

      const sourceField = el("input", {type:"text", value: String(orig.sourceField || orig.source_field || ""), placeholder:"表A字段名(如 选择屏数)"});
      const targetParam = el("input", {type:"text", value: String(orig.targetParam || orig.target_param || "prompt"), placeholder:"目标参数名(如 prompt)"});
      const split = el("input", {type:"checkbox", class:"switch"});
      split.checked = (orig.split == null) ? true : !!orig.split;
      const strict = el("input", {type:"checkbox", class:"switch"});
      strict.checked = (orig.strict == null) ? true : !!orig.strict;
      const enableItemParamMap = el("input", {type:"checkbox", class:"switch"});
      enableItemParamMap.checked = (orig.enableItemParamMap == null && orig.enable_item_param_map == null) ? true : !!(orig.enableItemParamMap ?? orig.enable_item_param_map);
      const enablePromptFields = el("input", {type:"checkbox", class:"switch"});
      enablePromptFields.checked = (orig.enablePromptFields == null && orig.enable_prompt_fields == null) ? true : !!(orig.enablePromptFields ?? orig.enable_prompt_fields);
      const maxItems = el("input", {type:"text", value: (orig.maxItems != null) ? String(orig.maxItems) : ((orig.max_items != null) ? String(orig.max_items) : ""), placeholder:"20"});
      const targetTableKey = el("input", {type:"text", value: String(orig.targetTableKey || orig.target_table_key || ""), placeholder:"目标表key"});
      const targetMatchField = el("input", {type:"text", value: String(orig.targetMatchField || orig.target_match_field || ""), placeholder:"匹配字段名(如 类型)"});
      const targetAppToken = el("input", {type:"text", value: String(orig.targetAppToken || orig.target_app_token || orig.app_token || ""), placeholder:"app_token(可选)"});
      const targetTableId = el("input", {type:"text", value: String(orig.targetTableId || orig.target_table_id || orig.table_id || ""), placeholder:"table_id(可选)"});
      const joinWith = el("input", {type:"text", value: String(orig.joinWith || orig.join_with || "\\n"), placeholder:"\\n"});

      const ipm = orig.itemParamMap || orig.item_param_map || orig.item_params || {};
      const itemParamMap = kvEditor(ipm, "param(如 prompt_general)", "字段名(如 通用总控提示词)");
      const pf = orig.promptFields || orig.prompt_fields || [];
      const promptFields = listEditor(pf, "字段名(如 通用总控提示词)");

      const form = el("div", {class:"form"}, [
        el("div", {class:"row2"}, [
          el("div", {class:"field"}, [el("div", {class:"label", text:"sourceField"}), sourceField, el("div", {class:"help", text:"表A字段名，支持record_id或文字匹配。"})]),
          el("div", {class:"field"}, [el("div", {class:"label", text:"maxItems"}), maxItems, el("div", {class:"help", text:"最多处理多少条匹配结果。"})])
        ]),
        el("div", {class:"row2"}, [
          el("div", {class:"field"}, [el("div", {class:"label", text:"split"}), split, el("div", {class:"help", text:"匹配到几条就提交几次。"})]),
          el("div", {class:"field"}, [el("div", {class:"label", text:"strict"}), strict, el("div", {class:"help", text:"匹配不到时直接报错。"})])
        ]),
        el("div", {class:"row2"}, [
          el("div", {class:"field"}, [el("div", {class:"label", text:"targetTableKey（推荐）"}), targetTableKey, el("div", {class:"help", text:"目标表在tables里的key。"})]),
          el("div", {class:"field"}, [el("div", {class:"label", text:"targetMatchField（可选）"}), targetMatchField, el("div", {class:"help", text:"当sourceField只有文字时的匹配字段。"})])
        ]),
        el("div", {class:"row2"}, [
          el("div", {class:"field"}, [el("div", {class:"label", text:"targetAppToken（可选）"}), targetAppToken, el("div", {class:"help", text:"不使用targetTableKey时填写。"})]),
          el("div", {class:"field"}, [el("div", {class:"label", text:"targetTableId（可选）"}), targetTableId, el("div", {class:"help", text:"不使用targetTableKey时填写。"})])
        ]),
        el("div", {class:"subBlock"}, [
          el("div", {class:"subHeader"}, [el("div", {class:"subTitle", text:"itemParamMap（不拼接）"}), enableItemParamMap]),
          el("div", {class:"relBody"}, [itemParamMap])
        ]),
        el("div", {class:"subBlock"}, [
          el("div", {class:"subHeader"}, [el("div", {class:"subTitle", text:"promptFields（拼接）"}), enablePromptFields]),
          el("div", {class:"relBody"}, [
            el("div", {class:"row2"}, [
              el("div", {class:"field"}, [el("div", {class:"label", text:"targetParam"}), targetParam, el("div", {class:"help", text:"拼接后的提示词写入到哪个参数。"})]),
              el("div", {class:"field"}, [el("div", {class:"label", text:"joinWith"}), joinWith, el("div", {class:"help", text:"拼接连接符，填\\n表示换行。"})])
            ]),
            promptFields
          ])
        ])
      ]);

      card.appendChild(head);
      card.appendChild(form);
      list.appendChild(card);
      updateEmptyVis();

      function applyVis() {
        const subBlocks = card.querySelectorAll(".subBlock");
        const itemBody = subBlocks[0] ? subBlocks[0].querySelector(".relBody") : null;
        const promptBody = subBlocks[1] ? subBlocks[1].querySelector(".relBody") : null;
        if (itemBody) itemBody.style.display = enableItemParamMap.checked ? "" : "none";
        if (promptBody) promptBody.style.display = enablePromptFields.checked ? "" : "none";
      }
      enableItemParamMap.onchange = applyVis;
      enablePromptFields.onchange = applyVis;
      applyVis();

      card._collect = () => {
        const sf = (sourceField.value || "").trim();
        if (!sf) return null;
        const rp = {};
        rp.sourceField = sf;
        const tp = (targetParam.value || "").trim();
        rp.targetParam = tp || "prompt";
        rp.split = !!split.checked;
        rp.strict = !!strict.checked;
        rp.enableItemParamMap = !!enableItemParamMap.checked;
        rp.enablePromptFields = !!enablePromptFields.checked;
        const mi = (maxItems.value || "").trim();
        if (mi) {
          const n = parseInt(mi, 10);
          if (!isNaN(n)) rp.maxItems = n;
        }
        const jw = (joinWith.value || "").trim();
        rp.joinWith = jw ? jw.replace(/\\n/g, "\n") : "\n";
        const ttk = (targetTableKey.value || "").trim();
        if (ttk) rp.targetTableKey = ttk;
        const tam = (targetAppToken.value || "").trim();
        if (tam) rp.targetAppToken = tam;
        const tti = (targetTableId.value || "").trim();
        if (tti) rp.targetTableId = tti;
        const tmf = (targetMatchField.value || "").trim();
        if (tmf) rp.targetMatchField = tmf;
        const ipmVal = rp.enableItemParamMap ? itemParamMap._getValue() : {};
        const ipmKeys = Object.keys(ipmVal || {});
        if (ipmKeys.length) rp.itemParamMap = ipmVal;
        const pfVal = rp.enablePromptFields ? promptFields._getValue() : [];
        if (pfVal.length) rp.promptFields = pfVal;
        return rp;
      };
    }

    const init = Array.isArray(initialPrompts) ? initialPrompts : ((initialPrompts && typeof initialPrompts === "object") ? [initialPrompts] : []);
    if (init.length) {
      for (const p of init) addPromptItem(p);
    }

    const addBtn = el("button", {type:"button", text:"添加子表", class:"btnGhost"});
    addBtn.onclick = () => {
      addPromptItem({});
      updateEmptyVis();
    };
    wrap.appendChild(helpEl);
    wrap.appendChild(list);
    wrap.appendChild(addBtn);
    updateEmptyVis();

    wrap._getValue = () => {
      const out = [];
      for (const card of Array.from(list.children)) {
        const p = card._collect();
        if (p) out.push(p);
      }
      return out;
    };

    wrap._setValue = (prompts) => {
      while (list.firstChild) list.removeChild(list.firstChild);
      const arr = Array.isArray(prompts) ? prompts : ((prompts && typeof prompts === "object") ? [prompts] : []);
      if (arr.length) {
        for (const p of arr) addPromptItem(p);
      }
      updateEmptyVis();
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

function commitCurrent(purpose) {
  if (!CURRENT || !CURRENT.commit) return true;
  try {
    CURRENT.commit(purpose);
    CURRENT = null;
    return true;
  } catch (e) {
    setStatus("当前配置有错误：" + String(e && e.message ? e.message : e), "err");
    return false;
  }
}

function select(type, key) {
  if (!commitCurrent("navigate")) return;
  STATE.selected = {type, key};
  renderSidebar();
  renderEditor();
}

function envSchemaFor(key) {
  return (STATE.envSchema && STATE.envSchema[key]) ? STATE.envSchema[key] : {};
}

function envSortKeys(keys) {
  const groups = Array.isArray(STATE.envGroupOrder) && STATE.envGroupOrder.length ? STATE.envGroupOrder : ["基础服务", "日志与调试", "ComfyUI", "远程回调", "RunningHub", "其它"];
  const groupIndex = {};
  groups.forEach((g, i) => { groupIndex[g] = i; });
  return keys.slice().sort((a, b) => {
    const ma = envSchemaFor(a);
    const mb = envSchemaFor(b);
    const ga = ma.group || "其它";
    const gb = mb.group || "其它";
    const ia = groupIndex.hasOwnProperty(ga) ? groupIndex[ga] : 999;
    const ib = groupIndex.hasOwnProperty(gb) ? groupIndex[gb] : 999;
    if (ia !== ib) return ia - ib;
    const oa = Number.isFinite(Number(ma.order)) ? Number(ma.order) : 9999;
    const ob = Number.isFinite(Number(mb.order)) ? Number(mb.order) : 9999;
    if (oa !== ob) return oa - ob;
    return String(a).localeCompare(String(b));
  });
}

function envControl(key, value, meta) {
  const type = String(meta.type || "text");
  if (type === "switch") {
    const input = el("input", {type:"checkbox", class:"switch"});
    const raw = String(value || "").trim().toLowerCase();
    input.checked = ["1", "true", "yes", "y", "on"].includes(raw);
    input._getValue = () => input.checked ? "1" : "0";
    return input;
  }
  if (type === "select") {
    const selectEl = el("select");
    selectEl.style.padding = "8px 10px";
    selectEl.style.width = "100%";
    selectEl.style.color = "var(--text)";
    selectEl.style.background = "rgba(255,255,255,0.06)";
    selectEl.style.border = "1px solid var(--border)";
    selectEl.style.borderRadius = "10px";
    const opts = Array.isArray(meta.options) && meta.options.length ? meta.options : [];
    const current = String(value || meta.default || "").trim().toUpperCase();
    for (const opt of opts) {
      const option = el("option", {value: String(opt), text: String(opt)});
      if (String(opt).toUpperCase() === current) option.selected = true;
      selectEl.appendChild(option);
    }
    selectEl._getValue = () => String(selectEl.value || "").trim().toUpperCase();
    return selectEl;
  }
  const input = el("input", {type:"text", value: value});
  input._getValue = () => input.value;
  return input;
}

function renderSidebar() {
  const envList = $("envList");
  envList.innerHTML = "";
  const keys = envSortKeys(Object.keys(STATE.env || {}));
  $("envCount").textContent = keys.length ? (keys.length + " 项") : "";
  let lastGroup = null;
  for (const k of keys) {
    const meta = envSchemaFor(k);
    const group = meta.group || "其它";
    if (group !== lastGroup) {
      envList.appendChild(el("div", {class:"help", style:"margin:8px 2px 2px; font-weight:650; color:rgba(229,231,235,0.86)", text:group}));
      lastGroup = group;
    }
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

  if (t === "env") {
    const v = (STATE.env && key in STATE.env) ? String(STATE.env[key] || "") : "";
    const meta = envSchemaFor(key);
    const desc = meta.description || ENV_DESC[key] || "配置说明待补充。";
    const input = envControl(key, v, meta);
    const readonlyHint = String(meta.readonlyHint || "").trim();
    const header = el("div", {class:"sectionHeader"}, [
      el("div", null, [el("h2", {text: key || ".env"}), el("div", {class:"help", text: desc})]),
    ]);
    const fieldChildren = [el("div", {class:"label", text:"值"}), input];
    if (readonlyHint) fieldChildren.push(el("div", {class:"help", text:readonlyHint}));
    const form = el("div", {class:"form"}, [
      el("div", {class:"field"}, fieldChildren),
    ]);
    root.appendChild(header);
    root.appendChild(form);
    CURRENT = {
      commit: () => {
        if (!key) return;
        STATE.env[key] = input._getValue ? input._getValue() : input.value;
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
    const triggerOrig0 = (orig.trigger && typeof orig.trigger === "object") ? orig.trigger : {};
    const triggerEnabled = el("input", {type:"checkbox", class:"switch"});
    triggerEnabled.checked = !!triggerOrig0.enabled;
    triggerEnabled.disabled = (mode === "write");
    const triggerWorkflow = el("input", {type:"text", value: String(triggerOrig0.workflow || orig.trigger_workflow || orig.triggerWorkflow || "")});
    const triggerUserField = el("input", {type:"text", value: String(triggerOrig0.user_field || triggerOrig0.userField || "")});
    const wfDataListId = "dl_table_trigger_workflow";
    triggerWorkflow.setAttribute("list", wfDataListId);
    const dlWf = el("datalist", {id: wfDataListId}, Object.keys((STATE.cfg && STATE.cfg.workflows) ? STATE.cfg.workflows : {}).sort().map(k2 => el("option", {value: k2})));
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
            el("span", {class:"pill", text:"触发"}),
            el("div", {class:"blockTitleText", text:"trigger"}),
            el("div", {class:"blockTitleSub", text:"开关 + 选择触发时执行的 workflow"}),
          ]),
        ]),
        (() => {
          const cfgWrap = el("div", {class:"form"}, [
            el("div", {class:"field"}, [
              el("div", {class:"label", text:"启用"}),
              triggerEnabled,
              el("div", {class:"help", text:(mode === "write") ? "BITABLE_MODE=write：不会订阅/处理表格事件，不支持触发。" : "开启后：当记录状态变为 trigger（默认“触发执行”）时，自动执行所选 workflow。"}),
            ]),
            el("div", {class:"field"}, [
              el("div", {class:"label", text:"workflow"}),
              triggerWorkflow,
              el("div", {class:"help", text:"要触发执行的 workflow key（对应 workflows）。留空会自动按该表匹配 default_workflow。"}),
            ]),
            el("div", {class:"field"}, [
              el("div", {class:"label", text:"user_field（可选）"}),
              triggerUserField,
              el("div", {class:"help", text:"可选：表格里“触发人”这一列的列名。作用：当事件里拿不到 operator_open_id 时，从该列读取 open_id 来触发执行。建议把这列做成【人员】类型字段。留空表示：必须依赖事件自带的 operator_open_id。"}),
            ]),
          ]);
          function applyVis() {
            const show = triggerEnabled.checked;
            triggerWorkflow.parentElement.style.display = show ? "" : "none";
            triggerUserField.parentElement.style.display = show ? "" : "none";
          }
          triggerEnabled.onchange = applyVis;
          applyVis();
          return cfgWrap;
        })(),
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
          el("div", {class:"help", text:"字段映射：key 为系统字段名，value 为多维表格列名。KEY 支持下拉选择，也可手填。结果列：output 是通用结果列；运行记录表（runLogTable）可用 output_image/output_video/output_audio/output_text 按类型分流，只填 output 时写同一列，只填某个 output_* 时只写对应类型，output 和任意 output_* 同时填写时分流列优先生效，output 只在四个 output_* 都未配置时兜底。主任务表如需分流，请在 workflow.writeBackFields 的 output 写 {\"image\":\"图片结果\",\"text\":\"文本结果\"}。其它系统字段：status（任务状态）、error（错误信息）、prompt_id（任务ID）、created_time（创建时间）、task_name（标题/任务名称，用于结果文件夹命名）。"}),
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
    root.appendChild(dlWf);

    CURRENT = {
      commit: () => {
        const newKey = (keyInput.value || "").trim();
        if (!newKey) throw new Error("table key 不能为空");
        const out = deepCopy(orig);
        out.app_token = (appToken.value || "").trim();
        out.table_id = (tableId.value || "").trim();
        out.view_id = (viewId.value || "").trim();
        const trigWf = (triggerWorkflow.value || "").trim();
        const trigUserField = (triggerUserField.value || "").trim();
        const trigEnabled = !!triggerEnabled.checked;
        if (trigEnabled || trigWf || trigUserField) {
          const obj = {enabled: trigEnabled};
          if (trigWf) obj.workflow = trigWf;
          if (trigUserField) obj.user_field = trigUserField;
          out.trigger = obj;
        }
        else delete out.trigger;
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
    const splitTaskLimit = el("input", {type:"text", value: String((orig.splitTaskLimit != null) ? orig.splitTaskLimit : (orig.split_task_limit != null ? orig.split_task_limit : "")), placeholder:"50"});
    const splitTaskLimitStrategy = el("select", null, [
      el("option", {value:"truncate", text:"truncate"}),
      el("option", {value:"error", text:"error"}),
    ]);
    splitTaskLimitStrategy.value = String(orig.splitTaskLimitStrategy || orig.split_task_limit_strategy || "truncate").trim().toLowerCase() === "error" ? "error" : "truncate";
    const defaults = kvEditor(orig.defaults || {}, "key", "value");
    const recordFields = kvEditor(orig.recordFields || orig.record_fields || {}, "参数名(如 prompt)", "列名(如 提示词)");
    const writeBackFields = kvEditor(orig.writeBackFields || orig.write_back_fields || {}, "字段key(如 output)", "列名(如 生成结果)");
    const oniOrig = orig.outputNodeIds || orig.output_node_ids || {};
    const outputNodeIds = kvEditor(
      Object.fromEntries(Object.entries(oniOrig).map(([k,v]) => [k, Array.isArray(v) ? v.join(",") : String(v)])),
      "类型(images/text/videos/gifs/files)",
      "节点ID(逗号分隔, 如 39,52)"
    );
    const params = paramsEditor(orig.params || {});
    const strictParamValidation = el("input", {type:"checkbox", class:"switch"});
    strictParamValidation.checked = false;
    const runninghubWorkflowId = el("input", {type:"text", value: (orig.runninghub && orig.runninghub.workflowId) ? String(orig.runninghub.workflowId) : ""});

    const relOrig = orig.relationPrompts || orig.relation_prompts || orig.relationPrompt || orig.relation_prompt || [];
    const relationPrompts = relationPromptsEditor(relOrig);
    const workflowTableBindingEnabled = el("input", {type:"checkbox", class:"switch"});
    const TABLE_BINDING_BACKUP_KEY = "_tableBindingBackup";
    const TB_BACKUP_LS_PREFIX = "wf_table_binding_backup::";
    function _loadTableBindingBackupFromLocalStorage(wfKey0) {
      const k0 = String(wfKey0 || "").trim();
      if (!k0) return null;
      try {
        const raw = String(localStorage.getItem(TB_BACKUP_LS_PREFIX + k0) || "");
        if (!raw.trim()) return null;
        const obj = JSON.parse(raw);
        return (obj && typeof obj === "object") ? obj : null;
      } catch (e) {
        return null;
      }
    }
    function _saveTableBindingBackupToLocalStorage(wfKey0, backup0) {
      const k0 = String(wfKey0 || "").trim();
      if (!k0) return;
      try {
        if (backup0 && typeof backup0 === "object") localStorage.setItem(TB_BACKUP_LS_PREFIX + k0, JSON.stringify(backup0));
        else localStorage.removeItem(TB_BACKUP_LS_PREFIX + k0);
      } catch (e) {}
    }
    function _getWorkflowTableBindingBackupFromOrig(orig0) {
      if (!(orig0 && typeof orig0 === "object")) return null;
      return orig0[TABLE_BINDING_BACKUP_KEY] || orig0._workflowTableBindingBackup || orig0._workflow_table_binding_backup || null;
    }
    function _extractWorkflowTableBindingFromWorkflowSpec(wfSpec0) {
      const wf0 = wfSpec0 && typeof wfSpec0 === "object" ? wfSpec0 : {};
      const out = {};
      const tableV = String(wf0.table || "").trim();
      if (tableV) out.table = tableV;
      const rlt = String(wf0.runLogTable || wf0.run_log_table || wf0.runLogTableKey || wf0.run_log_table_key || "").trim();
      if (rlt) out.runLogTable = rlt;
      const stl = String(wf0.splitTaskLimit != null ? wf0.splitTaskLimit : (wf0.split_task_limit != null ? wf0.split_task_limit : "")).trim();
      if (stl) out.splitTaskLimit = stl;
      const stls = String(wf0.splitTaskLimitStrategy || wf0.split_task_limit_strategy || "").trim();
      if (stls) out.splitTaskLimitStrategy = stls;
      const rf = wf0.recordFields || wf0.record_fields || {};
      if (rf && typeof rf === "object" && Object.keys(rf).length) out.recordFields = deepCopy(rf);
      const wb = wf0.writeBackFields || wf0.write_back_fields || {};
      if (wb && typeof wb === "object" && Object.keys(wb).length) out.writeBackFields = deepCopy(wb);
      const oni = wf0.outputNodeIds || wf0.output_node_ids || null;
      if (oni && typeof oni === "object" && Object.keys(oni).length) out.outputNodeIds = deepCopy(oni);
      const rps = wf0.relationPrompts || wf0.relation_prompts || wf0.relationPrompt || wf0.relation_prompt || null;
      if (rps && (Array.isArray(rps) || (typeof rps === "object" && Object.keys(rps).length))) out.relationPrompts = deepCopy(rps);
      const hasAny = Object.keys(out).length > 0;
      return hasAny ? out : null;
    }
    let tbBackup = _loadTableBindingBackupFromLocalStorage(key) || _getWorkflowTableBindingBackupFromOrig(orig) || null;
    workflowTableBindingEnabled.checked = !!(
      String(orig.table || "").trim() ||
      String(orig.runLogTable || orig.run_log_table || orig.runLogTableKey || orig.run_log_table_key || "").trim() ||
      Object.keys(orig.recordFields || orig.record_fields || {}).length ||
      Object.keys(orig.writeBackFields || orig.write_back_fields || {}).length ||
      (relOrig && typeof relOrig === "object" && Object.keys(relOrig).length)
    );
    function _hasAnyTableBindingInputValue() {
      const hasText = (s) => !!String(s || "").trim();
      const hasObj = (o) => !!(o && typeof o === "object" && Object.keys(o).length);
      const rf = recordFields && recordFields._getValue ? recordFields._getValue() : {};
      const wf = writeBackFields && writeBackFields._getValue ? writeBackFields._getValue() : {};
      const rps = relationPrompts && relationPrompts._getValue ? relationPrompts._getValue() : [];
      const hasRel = Array.isArray(rps) && rps.length > 0;
      return (
        hasText(tableKey.value) ||
        hasText(runLogTableKey.value) ||
        hasText(splitTaskLimit.value) ||
        hasObj(rf) ||
        hasObj(wf) ||
        hasRel
      );
    }
    function _buildTableBindingBackup() {
      const hasText = (s) => !!String(s || "").trim();
      const backup = {};
      const tableV = (tableKey.value || "").trim();
      if (tableV) backup.table = tableV;
      const rlt = (runLogTableKey.value || "").trim();
      if (rlt) backup.runLogTable = rlt;
      const stl = (splitTaskLimit.value || "").trim();
      if (stl) backup.splitTaskLimit = stl;
      const stls = (splitTaskLimitStrategy.value || "").trim();
      if (stls) backup.splitTaskLimitStrategy = stls;
      const rf = recordFields && recordFields._getValue ? recordFields._getValue() : {};
      if (rf && typeof rf === "object" && Object.keys(rf).length) backup.recordFields = rf;
      const wf = writeBackFields && writeBackFields._getValue ? writeBackFields._getValue() : {};
      if (wf && typeof wf === "object" && Object.keys(wf).length) backup.writeBackFields = wf;
      const oniRaw = outputNodeIds && outputNodeIds._getValue ? outputNodeIds._getValue() : {};
      if (oniRaw && typeof oniRaw === "object" && Object.keys(oniRaw).length) backup.outputNodeIds = oniRaw;
      const rps = relationPrompts && relationPrompts._getValue ? relationPrompts._getValue() : [];
      if (Array.isArray(rps) && rps.length) backup.relationPrompts = rps;
      const hasAny = Object.keys(backup).some((k) => {
        const v = backup[k];
        if (typeof v === "string") return hasText(v);
        if (Array.isArray(v)) return v.length > 0;
        return !!(v && typeof v === "object" && Object.keys(v).length);
      });
      return hasAny ? backup : null;
    }
    function _applyTableBindingBackupToInputs(backup) {
      const b = backup && typeof backup === "object" ? backup : null;
      if (!b) return;
      const hasText = (s) => !!String(s || "").trim();
      if (b.table != null && !hasText(tableKey.value)) tableKey.value = String(b.table);
      if (b.runLogTable != null && !hasText(runLogTableKey.value)) runLogTableKey.value = String(b.runLogTable);
      if (b.splitTaskLimit != null && !hasText(splitTaskLimit.value)) splitTaskLimit.value = String(b.splitTaskLimit);
      if (b.splitTaskLimitStrategy != null) {
        const vv = String(b.splitTaskLimitStrategy || "").trim().toLowerCase();
        splitTaskLimitStrategy.value = vv === "error" ? "error" : "truncate";
      }
      if (b.recordFields && recordFields && recordFields._setValue) recordFields._setValue(b.recordFields);
      if (b.writeBackFields && writeBackFields && writeBackFields._setValue) {
        const wb = deepCopy(b.writeBackFields);
        if (wb.output && typeof wb.output === "object" && !Array.isArray(wb.output)) {
          wb.output = JSON.stringify(wb.output);
        }
        writeBackFields._setValue(wb);
      }
      if (b.outputNodeIds && outputNodeIds && outputNodeIds._setValue) {
        const oni = {};
        for (const [k, v] of Object.entries(b.outputNodeIds || {})) {
          oni[k] = Array.isArray(v) ? v.join(",") : String(v);
        }
        outputNodeIds._setValue(oni);
      }
      const rps = b.relationPrompts || b.relationPrompt || b.relation_prompts || b.relation_prompt || [];
      if (relationPrompts && relationPrompts._setValue) {
        relationPrompts._setValue(rps);
      }
    }
    function _clearTableBindingInputs() {
      tableKey.value = "";
      runLogTableKey.value = "";
      splitTaskLimit.value = "";
      splitTaskLimitStrategy.value = "truncate";
      if (recordFields && recordFields._setValue) recordFields._setValue({});
      if (writeBackFields && writeBackFields._setValue) writeBackFields._setValue({});
      if (relationPrompts && relationPrompts._setValue) relationPrompts._setValue([]);
    }

    function applyWorkflowTableBindingEnabled() {
      const show = bitableEnabled && workflowTableBindingEnabled.checked;
      for (const it of Array.from(root.querySelectorAll(".workflowTableOnly"))) it.style.display = show ? "" : "none";
    }
    workflowTableBindingEnabled.onchange = () => {
      if (workflowTableBindingEnabled.checked) {
        const b = _loadTableBindingBackupFromLocalStorage(key) || _getWorkflowTableBindingBackupFromOrig(orig) || null;
        if (!_hasAnyTableBindingInputValue()) _applyTableBindingBackupToInputs(b);
      } else {
        const b = _buildTableBindingBackup();
        tbBackup = b;
        _saveTableBindingBackupToLocalStorage(key, b);
        _clearTableBindingInputs();
      }
      applyWorkflowTableBindingEnabled();
    };

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
        el("div", {class:"label", text:"表格绑定"}),
        workflowTableBindingEnabled,
        el("div", {class:"help", text:"总开关。关闭时会隐藏并在保存时清空 table、runLogTable、recordFields、writeBackFields、relationPrompts 这些表格相关配置。"}),
      ]),
      el("div", {class:"field bitableOnly workflowTableOnly"}, [el("div", {class:"label", text:"table"}), tableKey, el("div", {class:"help", text:"绑定的表格 key（对应 tables）。"} )]),
      el("div", {class:"field bitableOnly workflowTableOnly"}, [el("div", {class:"label", text:"runLogTable（运行记录表）"}), runLogTableKey, el("div", {class:"help", text:"把每个子任务的提交/成功/失败/结果写到这张表。填 tables 里的 key（例如 runlog_table）。留空表示不记录。"} )]),
      el("div", {class:"row2 bitableOnly workflowTableOnly"}, [
        el("div", {class:"field"}, [el("div", {class:"label", text:"splitTaskLimit"}), splitTaskLimit, el("div", {class:"help", text:"最终最多生成多少个子任务。多张副表 split 时按笛卡尔积后的总数来算。默认 50。"})]),
        el("div", {class:"field"}, [el("div", {class:"label", text:"splitTaskLimitStrategy"}), splitTaskLimitStrategy, el("div", {class:"help", text:"truncate=超出后只保留前 N 个；error=超出后直接报错，不偷偷少跑。推荐重要任务用 error。"})]),
      ]),
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
          el("div", {class:"help", text:"例：output -> 结果图，会把所有产出写入同一列；或 output -> {\"image\":\"图片结果\",\"text\":\"文本结果\"} 按类型分流。workflow 里的 writeBackFields 会覆盖表 fields；当 output 使用对象分流时，只写对象里配置的类型列，普通 output 兜底列不再生效。prompt_id -> 任务ID；status -> 任务状态。"}),
        ]),
      ]),
      el("div", {class:"block bitableOnly workflowTableOnly"}, [
        el("div", {class:"blockTitle"}, [
          el("div", {class:"blockTitleLeft"}, [
            el("span", {class:"pill", text:"过滤"}),
            el("div", {class:"blockTitleText", text:"outputNodeIds"}),
            el("div", {class:"blockTitleSub", text:"只采集指定节点的输出（可选，不填=全采）"}),
          ]),
        ]),
        el("div", {class:"form"}, [
          outputNodeIds,
          el("div", {class:"help", text:"例：images -> \"145,52\"（只采集节点145和52的图片）；text -> \"39\"（只采集节点39的文本）。支持 images/gifs/videos/text/files 五种类型。填节点id，逗号分隔。"}),
        ]),
      ]),
      el("div", {class:"block bitableOnly workflowTableOnly"}, [
        el("div", {class:"blockTitle"}, [
          el("div", {class:"blockTitleLeft"}, [
            el("span", {class:"pill", text:"子表"}),
            el("div", {class:"blockTitleText", text:"relationPrompts"}),
            el("div", {class:"blockTitleSub", text:"配置多张子表，支持笛卡尔积 split 提交多个任务"}),
          ]),
        ]),
        el("div", {class:"form"}, [
          relationPrompts,
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
      commit: (purpose) => {
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
          const stl = (splitTaskLimit.value || "").trim();
          if (stl) {
            const n = Number(stl);
            if (!Number.isFinite(n) || n < 1) throw new Error("splitTaskLimit 必须是大于等于 1 的数字");
            out.splitTaskLimit = Math.floor(n);
          } else {
            delete out.splitTaskLimit;
          }
          const stls = String(splitTaskLimitStrategy.value || "truncate").trim().toLowerCase();
          out.splitTaskLimitStrategy = stls === "error" ? "error" : "truncate";
          out.recordFields = recordFields._getValue();
          out.writeBackFields = writeBackFields._getValue();
          const wbOutput = out.writeBackFields.output;
          if (typeof wbOutput === "string" && wbOutput.trim().startsWith("{")) {
            try { const p = JSON.parse(wbOutput.trim()); if (p && typeof p === "object" && !Array.isArray(p)) { out.writeBackFields.output = p; } } catch (_) {}
          }

          const rps = relationPrompts._getValue();
          if (rps && Array.isArray(rps) && rps.length) {
            out.relationPrompts = rps;
          } else {
            delete out.relationPrompts;
          }
          // 删除旧格式的 relationPrompt
          delete out.relationPrompt;
          delete out.relation_prompt;
        } else {
          delete out.table;
          delete out.runLogTable;
          delete out.splitTaskLimit;
          delete out.splitTaskLimitStrategy;
          delete out.recordFields;
          delete out.writeBackFields;
          delete out.outputNodeIds;
          delete out.relationPrompts;
          delete out.relationPrompt;
          delete out.relation_prompt;
        }
        const purposeV = String(purpose || "").trim().toLowerCase();
        const isSave = purposeV === "save";
        if (isSave) {
          const nextBinding = _extractWorkflowTableBindingFromWorkflowSpec(out);
          if (!nextBinding) {
            const candidate = _loadTableBindingBackupFromLocalStorage(key) || _getWorkflowTableBindingBackupFromOrig(orig) || _extractWorkflowTableBindingFromWorkflowSpec(orig) || null;
            if (!out[TABLE_BINDING_BACKUP_KEY] && candidate) out[TABLE_BINDING_BACKUP_KEY] = candidate;
          }
          try { _saveTableBindingBackupToLocalStorage(newKey, out[TABLE_BINDING_BACKUP_KEY] || null); } catch (e) {}
        }
        const oniRaw = outputNodeIds._getValue();
        const oni = {};
        for (const [k, v] of Object.entries(oniRaw || {})) {
          const ids = String(v || "").split(",").map(s => s.trim()).filter(Boolean);
          if (ids.length) oni[k] = ids;
        }
        if (Object.keys(oni).length) out.outputNodeIds = oni; else delete out.outputNodeIds;

        out.params = params._getValue({strictEmptyName: strictParamValidation.checked});

        const rhId = (runninghubWorkflowId.value || "").trim();
        if (rhId) out.runninghub = {...(out.runninghub||{}), workflowId: rhId};
        else if (out.runninghub && Object.keys(out.runninghub).length === 1 && out.runninghub.workflowId) delete out.runninghub;
        else if (out.runninghub) delete out.runninghub.workflowId;

        if (newKey !== key) {
          try {
            const fromK = String(key || "").trim();
            const toK = String(newKey || "").trim();
            if (fromK && toK) {
              const raw = String(localStorage.getItem(TB_BACKUP_LS_PREFIX + fromK) || "");
              if (raw.trim() && !String(localStorage.getItem(TB_BACKUP_LS_PREFIX + toK) || "").trim()) {
                localStorage.setItem(TB_BACKUP_LS_PREFIX + toK, raw);
              }
              localStorage.removeItem(TB_BACKUP_LS_PREFIX + fromK);
            }
          } catch (e) {}
          delete wfs[key];
        }
        wfs[newKey] = out;
        STATE.selected = {type:"workflow", key:newKey};
      }
    };
    applyStrictParamValidationTone();
    applyWorkflowTableBindingEnabled();
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
  STATE.envSchema = (env.meta && env.meta.env_schema) ? env.meta.env_schema : {};
  STATE.envGroupOrder = (env.meta && Array.isArray(env.meta.env_group_order)) ? env.meta.env_group_order : [];

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

  const firstEnv = envSortKeys(Object.keys(STATE.env || {}))[0] || "";
  if (!STATE.selected.key) STATE.selected = {type:"env", key:firstEnv};

  renderSidebar();
  renderEditor();

  if (wf.admin_token_missing) setStatus("ADMIN_TOKEN 未设置：仅允许本机访问", "warn");
  else setStatus("已加载", "ok");
}

async function saveAll() {
  try {
    setStatus("保存中...", "muted");
    if (!commitCurrent("save")) return;
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
  if (!commitCurrent("navigate")) return;
  const key = "new_table";
  const tables = STATE.cfg.tables || {};
  if (!tables[key]) tables[key] = {app_token:"", table_id:"", view_id:"", fields:{}, status_values:{}};
  select("table", key);
};
$("btnAddWorkflow").onclick = () => {
  if (!commitCurrent("navigate")) return;
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
