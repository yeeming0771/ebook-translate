"""Install the agent skill and register only this project's Codex MCP server."""

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def install(codex_home=None):
    root = Path(__file__).resolve().parents[1]
    home = Path(codex_home or os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).resolve()
    executable = shutil.which("codex.cmd") if os.name == "nt" else shutil.which("codex")
    if not executable:
        raise RuntimeError("没有找到 Codex CLI，请先安装宿主提供的 Codex CLI")
    destination = home / "skills" / "ebook-translate"
    receipt = destination / ".ebook-translate-install.json"
    source = root / "skills" / "ebook-translate"
    files = {str(p.relative_to(source)).replace('\\', '/'): p for p in source.rglob('*') if p.is_file()}
    if destination.exists():
        if not receipt.is_file():
            raise RuntimeError("已存在同名 skill，未覆盖用户文件")
        previous = json.loads(receipt.read_text(encoding="utf-8"))
        for name, expected in previous["hashes"].items():
            path = destination / name
            if not path.is_file() or sha256(path.read_bytes()).hexdigest() != expected:
                raise RuntimeError(f"已安装 skill 存在用户修改，未覆盖：{name}")
        for name in files:
            if (destination / name).exists() and name not in previous["hashes"]:
                raise RuntimeError(f"目标存在非本工具管理文件，未覆盖：{name}")
    env = dict(os.environ, CODEX_HOME=str(home))
    launcher = str(root / "scripts" / "run_mcp.py")
    current = subprocess.run([executable, "mcp", "get", "ebook-translate", "--json"], capture_output=True, text=True, encoding="utf-8", env=env)
    if current.returncode == 0:
        configured = json.loads(current.stdout).get("transport", {})
        if configured.get("args") != [launcher] or Path(configured.get("command", "")).resolve() != Path(sys.executable).resolve():
            raise RuntimeError("已存在不同配置的 ebook-translate MCP，未覆盖")
    else:
        # Avoid interpreting arbitrary config/parsing failures as a missing server.
        if "No MCP server named" not in current.stderr:
            raise RuntimeError("无法确认现有 MCP 配置：" + current.stderr.strip())
        registered = subprocess.run([executable, "mcp", "add", "ebook-translate", "--", sys.executable, launcher], capture_output=True, text=True, encoding="utf-8", env=env)
        if registered.returncode:
            raise RuntimeError("MCP 注册失败：" + registered.stderr.strip())
    destination.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for name, path in files.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = path.read_bytes()
        target.write_bytes(payload)
        hashes[name] = sha256(payload).hexdigest()
    receipt.write_text(json.dumps({"repository": str(root), "hashes": hashes}, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"skill": str(destination), "mcp": "ebook-translate", "launcher": launcher,
            "next": "重新打开 Codex 会话以加载新的 MCP 与 skill，上传 EPUB 并说明目标语言。"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home")
    args = parser.parse_args()
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        print(json.dumps(install(args.codex_home), ensure_ascii=False, indent=2))
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
