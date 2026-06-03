import os
import sys
import re
import secrets
import subprocess
import socket

def read_dotenv(path):
    env_map = {}
    if not os.path.exists(path):
        return env_map
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip()
            if k:
                env_map[k] = v
    return env_map

def update_dotenv(path, updates):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    existing = {}
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _ = s.split("=", 1)
        existing[k.strip()] = True

    out_lines = []
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            out_lines.append(line)
            continue
        k, _ = s.split("=", 1)
        k_clean = k.strip()
        if k_clean in updates:
            out_lines.append(f"{k_clean}={updates[k_clean]}\n")
        else:
            out_lines.append(line)

    for k, v in updates.items():
        k_clean = k.strip()
        if k_clean and k_clean not in existing:
            out_lines.append(f"{k_clean}={v}\n")

    with open(path, "w", encoding="utf-8") as f:
        f.writelines(out_lines)

def resolve_system_python():
    try:
        res = subprocess.run(["where", "python"], capture_output=True, text=True, timeout=5)
        if res.returncode == 0:
            paths = res.stdout.strip().split("\n")
            if paths and os.path.exists(paths[0].strip()):
                return paths[0].strip()
    except Exception:
        pass
    try:
        res = subprocess.run(["where", "py"], capture_output=True, text=True, timeout=5)
        if res.returncode == 0:
            paths = res.stdout.strip().split("\n")
            if paths:
                py_path = paths[0].strip()
                res2 = subprocess.run([py_path, "-c", "import sys; print(sys.executable)"], capture_output=True, text=True, timeout=5)
                if res2.returncode == 0:
                    p = res2.stdout.strip()
                    if p and os.path.exists(p):
                        return p
    except Exception:
        pass
    return None

def fix_pyvenv_cfg(venv_root, sys_py=None):
    cfg_path = os.path.join(venv_root, "pyvenv.cfg")
    if not os.path.exists(cfg_path):
        return
    
    with open(cfg_path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()
    
    kv = {}
    for line in lines:
        if "=" in line:
            k, v = line.split("=", 1)
            kv[k.strip()] = v.strip()
    
    exe_in_cfg = kv.get("executable", "").strip().strip('"')
    need_fix = False
    if exe_in_cfg:
        if not os.path.exists(exe_in_cfg):
            need_fix = True
    else:
        need_fix = True
    
    if not need_fix:
        return
    
    candidates = []
    if sys_py:
        candidates.append(sys_py)
    
    user_profile = os.environ.get("USERPROFILE", "")
    if exe_in_cfg and user_profile:
        m = re.match(r'^C:\\Users\\[^\\]+\\(.+)$', exe_in_cfg, re.IGNORECASE)
        if m:
            rest = m.group(1)
            candidates.append(os.path.join(user_profile, rest))
    
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        for d in ["Python312", "Python311", "Python310"]:
            candidates.append(os.path.join(local_app_data, "Programs", "Python", d, "python.exe"))
    
    candidates.extend([
        "C:\\Program Files\\Python312\\python.exe",
        "C:\\Program Files\\Python311\\python.exe",
        "C:\\Program Files\\Python310\\python.exe",
        "C:\\Program Files (x86)\\Python312\\python.exe",
        "C:\\Program Files (x86)\\Python311\\python.exe",
        "C:\\Program Files (x86)\\Python310\\python.exe"
    ])
    
    picked_exe = None
    for c in candidates:
        if c and os.path.exists(c):
            picked_exe = c
            break
    
    if not picked_exe:
        return
    
    py_home_dir = os.path.dirname(picked_exe)
    
    ver = "3"
    try:
        res = subprocess.run([picked_exe, "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"], capture_output=True, text=True, timeout=5)
        if res.returncode == 0:
            ver = res.stdout.strip()
    except Exception:
        pass
    
    updates = {
        "home": py_home_dir,
        "executable": picked_exe,
        "version": ver,
        "command": f"{picked_exe} -m venv {venv_root}"
    }
    
    seen = {}
    out_lines = []
    for line in lines:
        if "=" in line:
            k, v = line.split("=", 1)
            k_clean = k.strip()
            if k_clean in updates:
                out_lines.append(f"{k_clean} = {updates[k_clean]}\n")
                seen[k_clean] = True
                continue
        out_lines.append(line)
    
    for k in ["home", "include-system-site-packages", "version", "executable", "command"]:
        if k in updates and k not in seen:
            out_lines.append(f"{k} = {updates[k]}\n")
    
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.writelines(out_lines)
    
    print(f"Fixed .venv\\pyvenv.cfg (executable) -> {picked_exe}")

def fix_venv_activation_scripts(venv_root):
    vr_win = os.path.abspath(venv_root)
    
    act_bat = os.path.join(venv_root, "Scripts", "activate.bat")
    if os.path.exists(act_bat):
        try:
            with open(act_bat, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
            changed = False
            out = []
            for line in lines:
                if line.strip().startswith("set VIRTUAL_ENV="):
                    out.append(f"set VIRTUAL_ENV={vr_win}\n")
                    changed = True
                else:
                    out.append(line)
            if changed:
                with open(act_bat, "w", encoding="utf-8") as f:
                    f.writelines(out)
        except Exception:
            pass
                
    act_sh = os.path.join(venv_root, "Scripts", "activate")
    if os.path.exists(act_sh):
        try:
            with open(act_sh, "r", encoding="utf-8", errors="ignore") as f:
                txt = f.read()
            txt2 = txt
            escaped_vr = vr_win.replace("\\", "\\\\")
            txt2 = re.sub(r'cygpath\s+"[^"]*?\\.venv"', f'cygpath "{escaped_vr}"', txt2)
            txt2 = re.sub(r'export\s+VIRTUAL_ENV="[^"]*?\\.venv"', f'export VIRTUAL_ENV="{escaped_vr}"', txt2)
            if txt2 != txt:
                with open(act_sh, "w", encoding="utf-8") as f:
                    f.write(txt2)
        except Exception:
            pass

def ensure_venv_module(venv_py, module_name, pip_package_name):
    try:
        res = subprocess.run([venv_py, "-c", f"import {module_name}"], capture_output=True, timeout=5)
        if res.returncode == 0:
            return
    except Exception:
        pass
    
    print(f"Installing missing dependency: {pip_package_name}")
    try:
        subprocess.run([venv_py, "-m", "pip", "install", "--upgrade", "pip"], capture_output=True)
    except Exception:
        pass
    subprocess.run([venv_py, "-m", "pip", "install", pip_package_name])

def fix_workflow_config_path(root_dir, env_map):
    raw = str(env_map.get("WORKFLOW_CONFIG_PATH", "") or "").strip()
    wf = raw.strip('"')
    config_dir = os.path.join(root_dir, "config")
    env_path = os.path.join(root_dir, ".env")

    # 相对路径 → 基于项目根目录解析为绝对路径
    if wf and not os.path.isabs(wf):
        wf = os.path.join(root_dir, wf)

    # 路径正确存在 → 确保写入绝对路径到 .env
    if wf and os.path.exists(wf):
        abs_path = os.path.abspath(wf)
        # 如果直接指向 example 文件，复制到 workflows.local.json 再使用
        example_path = os.path.join(config_dir, "workflows.example.json")
        local_path = os.path.join(config_dir, "workflows.local.json")
        if os.path.abspath(abs_path) == os.path.abspath(example_path) and not os.path.exists(local_path):
            import shutil
            shutil.copy2(example_path, local_path)
            abs_path = os.path.abspath(local_path)
            print(f"Created {local_path} from example")
        if abs_path != raw:
            update_dotenv(env_path, {"WORKFLOW_CONFIG_PATH": abs_path})
            env_map["WORKFLOW_CONFIG_PATH"] = abs_path
            print(f"WORKFLOW_CONFIG_PATH -> {abs_path}")
        return

    # 路径不存在 → 在项目 config 目录下查找替代文件
    cands = []
    if wf:
        leaf = os.path.basename(wf)
        if leaf:
            cands.append(os.path.join(config_dir, leaf))
    cands.append(os.path.join(config_dir, "workflows.local.json"))
    cands.append(os.path.join(config_dir, "workflows.example.json"))

    picked = ""
    for c in cands:
        if c and os.path.exists(c):
            picked = os.path.abspath(c)
            break

    if picked:
        # 如果找到的是 example 文件，复制到 workflows.local.json 再使用
        example_path = os.path.join(config_dir, "workflows.example.json")
        local_path = os.path.join(config_dir, "workflows.local.json")
        if os.path.abspath(picked) == os.path.abspath(example_path) and not os.path.exists(local_path):
            import shutil
            shutil.copy2(example_path, local_path)
            picked = os.path.abspath(local_path)
            print(f"Created {local_path} from example")
        update_dotenv(env_path, {"WORKFLOW_CONFIG_PATH": picked})
        env_map["WORKFLOW_CONFIG_PATH"] = picked
        print(f"WORKFLOW_CONFIG_PATH fixed -> {picked}")
    else:
        # 清空路径，让 bot 使用默认逻辑（config/workflows.local.json）
        update_dotenv(env_path, {"WORKFLOW_CONFIG_PATH": ""})
        env_map["WORKFLOW_CONFIG_PATH"] = ""
        print("WORKFLOW_CONFIG_PATH not found, cleared to allow startup.")

WORKFLOWS_LOCAL_SKELETON = """\
{
  "_comment": "精简配置骨架。完整示例参考 config/workflows.example.json",
  "default_table": "",
  "default_workflow": "",
  "tables": {},
  "automation": {},
  "workflows": {}
}
"""


def ensure_workflows_local_config(root_dir):
    """如果 workflows.local.json 不存在，从骨架自动创建一个精简副本"""
    local_path = os.path.join(root_dir, "config", "workflows.local.json")
    if not os.path.exists(local_path):
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, "w", encoding="utf-8") as f:
            f.write(WORKFLOWS_LOCAL_SKELETON.strip() + "\n")
        print("Created config\\workflows.local.json (minimal skeleton)")


REQUIRED_ENV_KEYS = [
    ("FEISHU_APP_ID", "飞书应用 App ID"),
    ("FEISHU_APP_SECRET", "飞书应用 App Secret"),
]

# 可选但推荐配置：留空则自动生成随机令牌
OPTIONAL_ENV_KEYS = [
    ("ADMIN_TOKEN", "管理后台访问令牌（留空自动生成）"),
]


def ensure_required_config(root_dir):
    """预检配置，缺失时交互式提示用户输入并写入 .env"""
    env_path = os.path.join(root_dir, ".env")
    env_map = read_dotenv(env_path)

    missing = []
    for key, desc in REQUIRED_ENV_KEYS:
        if not env_map.get(key, "").strip():
            missing.append((key, desc, True))
    for key, desc in OPTIONAL_ENV_KEYS:
        if not env_map.get(key, "").strip():
            missing.append((key, desc, False))

    if not missing:
        return True

    print("")
    print("=" * 60)
    print("  缺少配置，请按提示输入：")
    print("  (Ctrl+C 可取消)")
    print("=" * 60)

    updates = {}
    for key, desc, required in missing:
        print(f"\n  [{desc}]")
        try:
            value = input(f"  {key} = ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n\n  已取消。")
            return False
        if not value:
            if required:
                print(f"\n  ! {key} 不能为空，已取消。")
                return False
            value = secrets.token_urlsafe(16)
            print(f"  -> 已自动生成: {value}")
        updates[key] = value

    # 如果 .env 不存在，从 .env.example 复制
    if not os.path.exists(env_path):
        example_path = os.path.join(root_dir, ".env.example")
        if os.path.exists(example_path):
            import shutil
            shutil.copy2(example_path, env_path)

    update_dotenv(env_path, updates)

    for k, v in updates.items():
        os.environ[k] = v

    print(f"\n  已保存到 .env\n")
    return True


def check_port_in_use(host, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        s.bind(("127.0.0.1", port))
        s.close()
        return False
    except Exception:
        return True

def main():
    root = os.path.abspath(os.path.dirname(__file__))
    os.chdir(root)
    
    venv_py = os.path.join(root, ".venv", "Scripts", "python.exe")
    if not os.path.exists(venv_py):
        print("\nVirtual env (.venv) not found. Please run install.cmd first.")
        sys.exit(1)
        
    sys_py = resolve_system_python()
    try:
        fix_pyvenv_cfg(os.path.join(root, ".venv"), sys_py)
    except Exception as e:
        print(f"\nFix pyvenv.cfg failed: {e}")
        
    try:
        fix_venv_activation_scripts(os.path.join(root, ".venv"))
    except Exception:
        pass
        
    try:
        ensure_venv_module(venv_py, "multipart", "python-multipart")
    except Exception as e:
        print(f"\nDependency check failed: {e}")
        
    env_map = read_dotenv(os.path.join(root, ".env"))
    try:
        fix_workflow_config_path(root, env_map)
    except Exception:
        pass

    try:
        ensure_workflows_local_config(root)
    except Exception:
        pass

    cb_host = "127.0.0.1"
    cb_port = 9901
    if "CALLBACK_HOST" in env_map:
        h = env_map["CALLBACK_HOST"].strip().strip('"')
        if h:
            cb_host = h
    if "CALLBACK_PORT" in env_map:
        p = env_map["CALLBACK_PORT"].strip().strip('"')
        if p:
            try:
                cb_port = int(p)
            except Exception:
                pass
                
    if check_port_in_use(cb_host, cb_port):
        print(f"\nPort already in use: {cb_host}:{cb_port}")
        print("Please close the existing process, or change CALLBACK_PORT in .env, then run start.cmd again.")
        sys.exit(1)

    if not ensure_required_config(root):
        sys.exit(1)

    # Re-read .env to pick up any values written by interactive config
    env_map = read_dotenv(os.path.join(root, ".env"))

    print("Starting biaoge_bot ...")
    admin_token = env_map.get("ADMIN_TOKEN", "").strip()
    print(f"Config page: http://{cb_host}:{cb_port}/admin/config?token={admin_token}\n")
    
    try:
        # 正常调用虚拟环境运行机器人主程序
        subprocess.run([venv_py, "-m", "biaoge_bot.main"])
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
