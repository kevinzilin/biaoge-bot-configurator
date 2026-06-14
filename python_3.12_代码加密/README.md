# Python 3.12 代码加密（Nuitka）

本目录保存 Nuitka 编译缓存和历史 Windows 编译脚本。新的主入口是项目根目录下的 `scripts/build_protected_modules.py`，用于在当前平台把 `未加密源码` 中的受保护模块编译成可导入扩展模块。

- Windows 输出 `.pyd`
- macOS/Linux 输出 `.so`
- 编译产物与 Python 版本、系统、CPU 架构绑定，不能跨平台复用

推荐命令：

```bash
python scripts/build_protected_modules.py
```

Windows 如需额外指定 MSVC 参数：

```powershell
python scripts/build_protected_modules.py --nuitka-arg=--msvc=14.3
```

下面内容是旧版 Windows + Python 3.12 `.pyd` 编译说明，保留供排查兼容问题。

## 前置条件
- 已安装 Visual Studio Build Tools（C++ 桌面开发），并在 **Developer PowerShell for VS 2022** 中执行命令（确保 `cl.exe` 可用）

## 常用编译命令（biaoge_bot）
建议在项目根目录 `D:\www\biaoge` 执行。

定义编译用 Python：

```powershell
$py = "D:\www\biaoge\python_3.12_代码加密\.venv\Scripts\python.exe"
```

1) 编译授权模块：

```powershell
& $py -m nuitka --mode=module D:\www\biaoge\biaoge_bot\license_guard.py --output-dir=D:\www\biaoge\biaoge_bot --nofollow-imports --assume-yes-for-downloads --no-pyi-file --msvc=14.3
Copy-Item D:\www\biaoge\biaoge_bot\license_guard.cp312-win_amd64.pyd -Destination D:\www\biaoge\biaoge_bot\license_guard.pyd -Force
Remove-Item D:\www\biaoge\biaoge_bot\license_guard.cp312-win_amd64.pyd -Force
```

2) 编译 modules：

```powershell
& $py -m nuitka --mode=module D:\www\biaoge\biaoge_bot\modules\bitable.py --output-dir=D:\www\biaoge\biaoge_bot\modules --nofollow-imports --assume-yes-for-downloads --no-pyi-file --msvc=14.3
Copy-Item D:\www\biaoge\biaoge_bot\modules\bitable.cp312-win_amd64.pyd -Destination D:\www\biaoge\biaoge_bot\modules\bitable.pyd -Force
Remove-Item D:\www\biaoge\biaoge_bot\modules\bitable.cp312-win_amd64.pyd -Force

& $py -m nuitka --mode=module D:\www\biaoge\biaoge_bot\modules\drive.py --output-dir=D:\www\biaoge\biaoge_bot\modules --nofollow-imports --assume-yes-for-downloads --no-pyi-file --msvc=14.3
Copy-Item D:\www\biaoge\biaoge_bot\modules\drive.cp312-win_amd64.pyd -Destination D:\www\biaoge\biaoge_bot\modules\drive.pyd -Force
Remove-Item D:\www\biaoge\biaoge_bot\modules\drive.cp312-win_amd64.pyd -Force
```

## 代理（可选）
如果需要加速下载，可在执行 Nuitka 前设置：

```powershell
$env:HTTP_PROXY="http://127.0.0.1:1081"
$env:HTTPS_PROXY="http://127.0.0.1:1081"
$env:ALL_PROXY="socks5h://127.0.0.1:1080"
$env:NO_PROXY="localhost,127.0.0.1"
```

## 清理
- `*.build` 目录是中间产物，可删除
- `.nuitka_cache` 是缓存，可保留以加速二次编译，也可删除后重新生成
