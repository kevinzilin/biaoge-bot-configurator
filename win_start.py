import os
import sys
import re
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
    default_path = os.path.join(root_dir, "config", "workflows.local.json")

    if not wf:
        if os.path.exists(default_path):
            picked = os.path.abspath(default_path)
            update_dotenv(os.path.join(root_dir, ".env"), {"WORKFLOW_CONFIG_PATH": picked})
            env_map["WORKFLOW_CONFIG_PATH"] = picked
            print(f"WORKFLOW_CONFIG_PATH set -> {picked}")
        return
    if wf and not os.path.exists(wf):
        leaf = os.path.basename(wf) if wf else ""
        cands = []
        config_dir = os.path.join(root_dir, "config")
        if wf and not os.path.isabs(wf):
            cands.append(os.path.join(root_dir, wf))
        else:
            cands.append(wf)
        if leaf:
            cands.append(os.path.join(config_dir, leaf))
        cands.append(os.path.join(config_dir, "workflows.local.json"))
        cands.append(os.path.join(config_dir, "workflows.example.json"))
        
        picked = ""
        for c in cands:
            c_clean = c.strip().strip('"')
            if c_clean and os.path.exists(c_clean):
                picked = os.path.abspath(c_clean)
                break
        
        if picked:
            update_dotenv(os.path.join(root_dir, ".env"), {"WORKFLOW_CONFIG_PATH": picked})
            env_map["WORKFLOW_CONFIG_PATH"] = picked
            print(f"WORKFLOW_CONFIG_PATH fixed -> {picked}")
        else:
            update_dotenv(os.path.join(root_dir, ".env"), {"WORKFLOW_CONFIG_PATH": ""})
            env_map["WORKFLOW_CONFIG_PATH"] = ""
            print("WORKFLOW_CONFIG_PATH not found, cleared to allow startup.")

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
        
    print("Starting biaoge_bot ...")
    print(f"Config page: http://{cb_host}:{cb_port}/admin/config?token=<ADMIN_TOKEN>\n")
    
    try:
        # 正常调用虚拟环境运行机器人主程序
        subprocess.run([venv_py, "-m", "biaoge_bot.main"])
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
