#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# tools/vscode_mcp_server.py
# ──────────────────────────────────────────────────────────────────────────
#  VibeBridge VSCode MCP server (stdio JSON-RPC, same framing the bridge's
#  MCPClient already speaks: one JSON object per line on stdout).
#
#  This is the VSCode counterpart of Roblox's StudioMCP.exe: it advertises
#  the workspace toolset, and forwards editor-aware calls to the jhamama
#  "VS Code MCP Bridge" extension (http://127.0.0.1:3333/sse) when it is up,
#  falling back to local file/terminal tools when it is not.
#
#  NEVER print to stdout except protocol replies (the bridge parses every
#  line). Diagnostics go to stderr.
# ──────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import fnmatch
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import urllib.parse
from pathlib import Path

VERSION = "0.1.0"


def err(*a):
    print(*a, file=sys.stderr, flush=True)


# ── workspace root ────────────────────────────────────────────────────────
START_CWD = Path.cwd().resolve()


def _jhamama_workspace() -> Path | None:
    try:
        info = JH.call_json("get_workspace_info", {}, timeout=10)
        if isinstance(info, dict):
            folders = info.get("folders") or info.get("workspaceFolders") or []
            if folders:
                f0 = folders[0]
                cand = (f0.get("path") or f0.get("uri") or f0.get("fsPath")
                        if isinstance(f0, dict) else f0)
                if cand:
                    p = Path(str(cand)).expanduser()
                    if p.is_dir():
                        return p.resolve()
            for key in ("rootPath", "root", "path"):
                if info.get(key):
                    p = Path(str(info[key])).expanduser()
                    if p.is_dir():
                        return p.resolve()
    except Exception:
        pass
    return None


def resolve_root(explicit=None) -> Path:
    if explicit:
        p = Path(str(explicit)).expanduser()
        if p.is_dir():
            return p.resolve()
    env = os.environ.get("VIBEBRIDGE_ROOT")
    if env:
        p = Path(env).expanduser()
        if p.is_dir():
            return p.resolve()
    auto = _jhamama_workspace()
    if auto:
        return auto
    return START_CWD


def safe_path(root: Path, rel: str) -> Path:
    root = root.resolve()
    p = (root / rel).resolve() if not os.path.isabs(rel) else Path(rel).resolve()
    if os.environ.get("VIBEBRIDGE_ALLOW_OUTSIDE", "0") != "1":
        try:
            p.relative_to(root)
        except ValueError:
            raise ValueError(f"path escapes workspace root: {rel}")
    return p


def rel_str(root: Path, p: Path) -> str:
    try:
        return str(p.relative_to(root))
    except ValueError:
        return str(p)


# ── office-safe: secret blocklist + redaction (read-paths only) ──────────
# Blocked names are REFUSED outright (read/write/list/grep/diff/open).
# Redaction only ever applies to content ABOUT TO BE SENT to the AI
# (read/grep/terminal/diagnostics/active-file) - never to write_file or
# show_diff payloads, so files can never be corrupted by it.
BLOCKED_PATTERNS = (
    ".env", ".env.*",
    "*.pem", "*.key", "*.p12", "*.pfx", "*.asc", "*.gpg",
    "*secret*", "*credential*", "*passwd*", "*passwrd*",
    "id_rsa*", "id_ed25519*", "id_ecdsa*", "id_dsa*",
    ".npmrc", ".pypirc", ".netrc", "_netrc", ".git-credentials",
    "*token*.json", "serviceaccount*.json", "service-account*.json",
)


def blocked_reason(root: Path, rel_or_abs: str) -> str | None:
    """Why this path must not be touched, or None if allowed."""
    try:
        p = Path(rel_or_abs)
        rel = rel_str(root, p.resolve() if p.is_absolute() else (root / rel_or_abs).resolve())
    except Exception:
        rel = str(rel_or_abs)
    base = os.path.basename(rel).lower()
    rel_low = rel.lower()
    for pat in BLOCKED_PATTERNS:
        if fnmatch.fnmatchcase(base, pat) or fnmatch.fnmatchcase(rel_low, pat):
            return (f"blocked: '{rel}' looks like a secret/credential file "
                    f"(matched '{pat}'). VibeBridge refuses to open it - do NOT "
                    f"work around this; tell the user plainly.")
    return None


_SECRET_RES = [
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]+"),
    re.compile(r"sk-ant-[A-Za-z0-9\-_]+"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)(api[_-]?key|apikey|secret|passwd|password|token|aws_secret_access_key)"
               r"(\s*[=:]\s*['\"]?)([^\s'\";,}]{6,})(['\"]?)"),
]


def redact_text(text: str) -> tuple[str, int]:
    """Replace likely secrets with [REDACTED]; returns (text, count)."""
    if not isinstance(text, str):
        return text, 0
    count = 0

    def _kv(m):
        # count comes from subn's return value - do NOT increment here too
        return m.group(1) + m.group(2) + "[REDACTED]" + m.group(4)

    # key=value pairs first (consumes the whole assignment incl. separator),
    # then bare token shapes on whatever remains - avoids double-counting.
    text, n = _SECRET_RES[-1].subn(_kv, text)
    count += n
    for rx in _SECRET_RES[:-1]:
        text, n = rx.subn("[REDACTED]", text)
        count += n
    return text, count


def redact_obj(obj):
    """Redact secrets inside a JSON-able result; returns (obj, count)."""
    try:
        raw = json.dumps(obj, ensure_ascii=False)
    except Exception:
        return obj, 0
    red, n = redact_text(raw)
    if not n:
        return obj, 0
    try:
        return json.loads(red), n
    except Exception:
        return {"_redacted_text": red}, n


# ── jhamama HTTP/SSE client (fresh session per call: no stale sessions) ──
class _Jhamama:
    def __init__(self):
        raw = os.environ.get("VIBEBRIDGE_VSCODE_URL", "http://127.0.0.1:3333")
        u = urllib.parse.urlparse(raw)
        self.host = u.hostname or "127.0.0.1"
        self.port = u.port or 3333
        self.auth = os.environ.get("VIBEBRIDGE_VSCODE_TOKEN", "")

    def _auth_h(self) -> str:
        return f"Authorization: Bearer {self.auth}\r\n" if self.auth else ""

    def health(self, timeout=3.0):
        try:
            c = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
            c.request("GET", "/health", headers=(
                {"Authorization": f"Bearer {self.auth}"} if self.auth else {}))
            r = c.getresponse()
            body = r.read().decode("utf-8", "replace")
            c.close()
            if r.status == 200:
                return json.loads(body)
        except Exception:
            pass
        return None

    def is_up(self) -> bool:
        return self.health() is not None

    def _session_post(self, sock_file, msg_url: str, obj: dict, timeout=30.0):
        body = json.dumps(obj).encode()
        c = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        c.request("POST", msg_url, body=body, headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {self.auth}"} if self.auth else {}),
        })
        r = c.getresponse()
        r.read()
        c.close()
        if r.status not in (200, 202):
            raise RuntimeError(f"vscode mcp POST -> {r.status}")

    def _read_until_id(self, sock, sock_file, rid: int, timeout=60.0):
        import time
        deadline = time.time() + timeout
        buf: list[str] = []

        def flush_block() -> dict | None:
            raw = "\n".join(buf)
            buf.clear()
            datas = [l[5:].strip() for l in raw.split("\n") if l.startswith("data:")]
            if not datas:
                return None
            try:
                msg = json.loads("\n".join(datas))
            except Exception:
                return None
            return msg if msg.get("id") == rid else None

        sock.settimeout(max(1.0, deadline - time.time()))
        while time.time() < deadline:
            try:
                line = sock_file.readline()
            except Exception:
                return None
            if not line:
                return None
            line = line.strip()
            if line in ("", ":"):
                r = flush_block()
                if r is not None:
                    return r
                continue
            buf.append(line)
        return None

    def call(self, tool: str, args: dict, timeout=60.0) -> str:
        s = socket.create_connection((self.host, self.port), timeout=10)
        try:
            s.sendall(
                f"GET /sse HTTP/1.1\r\nHost: {self.host}:{self.port}\r\n"
                f"Accept: text/event-stream\r\nCache-Control: no-cache\r\n"
                f"{self._auth_h()}Connection: keep-alive\r\n\r\n".encode())
            f = s.makefile("r", encoding="utf-8", errors="replace")
            while True:
                line = f.readline()
                if not line or line in ("\r\n", "\n"):
                    break
            msg_url = None
            ev: list[str] = []
            import time
            t0 = time.time()
            while time.time() - t0 < 10:
                line = f.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    for l in ev:
                        if l.startswith("data:") and "/messages" in l:
                            msg_url = l[5:].strip()
                    ev = []
                    if msg_url:
                        break
                    continue
                ev.append(line)
            if not msg_url:
                raise RuntimeError("vscode mcp: no SSE endpoint")
            rid = 1
            self._session_post(f, msg_url, {"jsonrpc": "2.0", "id": rid, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                           "clientInfo": {"name": "vibebridge-vscode", "version": VERSION}}})
            init_res = self._read_until_id(s, f, rid, timeout=15)
            if init_res is None or init_res.get("error"):
                raise RuntimeError(f"vscode mcp initialize failed: {init_res}")
            self._session_post(f, msg_url, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            rid = 2
            self._session_post(f, msg_url, {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                "params": {"name": tool, "arguments": args or {}}})
            res = self._read_until_id(s, f, rid, timeout=timeout)
            if res is None:
                raise RuntimeError(f"vscode mcp: no response for {tool}")
            if res.get("error"):
                raise RuntimeError(str(res["error"]))
            parts = res.get("result", {}).get("content", [])
            return "\n".join(p.get("text", "") for p in parts
                             if isinstance(p, dict) and p.get("type") == "text")
        finally:
            try:
                s.close()
            except Exception:
                pass

    def call_json(self, tool: str, args: dict, timeout=60.0):
        try:
            return json.loads(self.call(tool, args, timeout=timeout))
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}


JH = _Jhamama()


# ── local tools ───────────────────────────────────────────────────────────
def t_read_file(a) -> dict:
    root = resolve_root(a.get("_root") or a.get("workspace_root"))
    path = a.get("path", "")
    blocked = blocked_reason(root, path)
    if blocked:
        return {"ok": False, "error": blocked}
    sl = max(1, int(a.get("start_line", 1)))
    ml = int(a.get("max_lines", 400))
    if JH.is_up() and not a.get("_via") == "local":
        try:
            ap = str(safe_path(root, path))
            out = JH.call_json("read_file", {"filePath": ap,
                                             "startLine": sl - 1, "endLine": sl - 1 + ml})
            if isinstance(out, dict) and out.get("ok") is False and "error" in out and "content" not in out and "text" not in out:
                raise RuntimeError(out.get("error"))
            out, n = redact_obj(out)
            r = {"ok": True, "_via": "vscode", "_root": str(root), "result": out}
            if n:
                r["redacted"] = n
            return r
        except Exception as e:
            err(f"[read_file] vscode failed, local fallback: {e}")
    try:
        fp = safe_path(root, path)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if not fp.is_file():
        return {"ok": False, "error": f"not a file: {path}"}
    try:
        lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as e:
        return {"ok": False, "error": str(e)}
    total = len(lines)
    chunk = lines[sl - 1: sl - 1 + ml]
    content, n = redact_text("\n".join(chunk))
    r = {"ok": True, "_via": "local", "_root": str(root), "path": rel_str(root, fp),
         "total_lines": total, "start_line": sl, "content": content,
         "truncated": (sl - 1 + ml) < total}
    if n:
        r["redacted"] = n
    return r


def t_write_file(a) -> dict:
    root = resolve_root(a.get("_root") or a.get("workspace_root"))
    if "path" not in a or "content" not in a:
        return {"ok": False, "error": "write_file needs path + content"}
    blocked = blocked_reason(root, a["path"])
    if blocked:
        return {"ok": False, "error": blocked}
    if JH.is_up() and not a.get("_via") == "local":
        try:
            ap = str(safe_path(root, a["path"]))
            out = JH.call_json("write_file", {"filePath": ap, "content": a["content"]})
            return {"ok": True, "_via": "vscode", "_root": str(root), "path": ap, "result": out}
        except Exception as e:
            err(f"[write_file] vscode failed, local fallback: {e}")
    try:
        fp = safe_path(root, a["path"])
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    try:
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(a["content"], encoding="utf-8")
        return {"ok": True, "_via": "local", "_root": str(root),
                "path": rel_str(root, fp), "bytes": len(a["content"].encode("utf-8"))}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_list_files(a) -> dict:
    root = resolve_root(a.get("_root") or a.get("workspace_root"))
    pattern = a.get("pattern", "**/*.py")
    maxn = int(a.get("max_results", 200))
    try:
        out = []
        hidden = 0
        for p in root.glob(pattern):
            if len(out) >= maxn:
                break
            if p.is_file():
                rel = rel_str(root, p)
                if blocked_reason(root, rel):
                    hidden += 1
                    continue
                out.append(rel)
        r = {"ok": True, "_root": str(root), "count": len(out), "files": sorted(out)}
        if hidden:
            r["hidden_sensitive"] = hidden
        return r
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_grep(a) -> dict:
    root = resolve_root(a.get("_root") or a.get("workspace_root"))
    if "query" not in a:
        return {"ok": False, "error": "grep needs query"}
    try:
        rx = re.compile(a["query"])
    except re.error as e:
        return {"ok": False, "error": f"bad regex: {e}"}
    include = a.get("include", "*")
    maxn = int(a.get("max_results", 50))
    hits = []
    skipped = 0
    redacted = 0
    for dirpath, _dirs, files in os.walk(root):
        if any(skip in dirpath for skip in (".git", "__pycache__", "node_modules", ".venv", "venv")):
            continue
        for fn in files:
            if not fnmatch.fnmatch(fn, include):
                continue
            full = Path(dirpath) / fn
            if blocked_reason(root, rel_str(root, full)):
                skipped += 1
                continue
            try:
                if full.stat().st_size > 1_000_000:
                    continue
                text = full.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    t, n = redact_text(line[:500])
                    redacted += n
                    hits.append({"file": rel_str(root, full), "line": i, "text": t})
                    if len(hits) >= maxn:
                        return {"ok": True, "_root": str(root), "count": len(hits),
                                "hits": hits, "skipped_sensitive": skipped, "redacted": redacted}
    return {"ok": True, "_root": str(root), "count": len(hits), "hits": hits,
            "skipped_sensitive": skipped, "redacted": redacted}


def t_run_terminal(a) -> dict:
    root = resolve_root(a.get("_root") or a.get("workspace_root"))
    if "command" not in a:
        return {"ok": False, "error": "run_terminal needs command"}
    command = a["command"]
    if any(b in command for b in ("rm -rf /", "rm -rf ~", ":(){:|:&};:", "mkfs", "dd if=")):
        return {"ok": False, "error": "blocked dangerous command"}
    if JH.is_up() and not a.get("_via") == "local":
        try:
            out = JH.call_json("run_terminal_command", {
                "command": command, "timeoutMs": int(a.get("timeout_sec", 30)) * 1000})
            out, n = redact_obj(out)
            r = {"ok": True, "_via": "vscode", "_root": str(root), "result": out}
            if n:
                r["redacted"] = n
            return r
        except Exception as e:
            err(f"[run_terminal] vscode failed, local fallback: {e}")
    workdir = root
    try:
        proc = subprocess.run(command, shell=True, cwd=str(workdir),
                              capture_output=True, text=True,
                              timeout=int(a.get("timeout_sec", 30)))
        out = (proc.stdout or "") + (proc.stderr or "")
        maxc = 20000
        trunc = len(out) > maxc
        if trunc:
            out = out[: maxc // 2] + "\n...[truncated]...\n" + out[-maxc // 2:]
        out, n = redact_text(out)
        r = {"ok": True, "_via": "local", "_root": str(root),
             "exit_code": proc.returncode, "output": out, "truncated": trunc}
        if n:
            r["redacted"] = n
        return r
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timeout after {a.get('timeout_sec', 30)}s"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _need_vscode():
    if not JH.is_up():
        return {"ok": False, "error": "VSCode MCP server is off - start it in VSCode (Cmd+Shift+P > 'VS Code MCP Bridge: Start Server')."}
    return None


def t_get_active_file(a) -> dict:
    miss = _need_vscode()
    if miss:
        return miss
    out, n = redact_obj(JH.call_json("get_active_file", {}))
    r = {"ok": True, "result": out}
    if n:
        r["redacted"] = n
    return r


def t_get_diagnostics(a) -> dict:
    miss = _need_vscode()
    if miss:
        return miss
    args = {}
    if a.get("filePath"):
        args["filePath"] = a["filePath"]
    if a.get("severity"):
        args["severity"] = a["severity"]
    out, n = redact_obj(JH.call_json("get_diagnostics", args))
    r = {"ok": True, "result": out}
    if n:
        r["redacted"] = n
    return r


def t_show_diff(a) -> dict:
    miss = _need_vscode()
    if miss:
        return miss
    if "path" not in a or "content" not in a:
        return {"ok": False, "error": "show_diff needs path + content"}
    root = resolve_root(a.get("_root") or a.get("workspace_root"))
    blocked = blocked_reason(root, a["path"])
    if blocked:
        return {"ok": False, "error": blocked}
    try:
        ap = str(safe_path(root, a["path"]))
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "result": JH.call_json("show_diff", {
        "filePath": ap, "newContent": a["content"],
        "title": a.get("title", "VibeBridge diff")})}


def t_open_file(a) -> dict:
    miss = _need_vscode()
    if miss:
        return miss
    if "path" not in a:
        return {"ok": False, "error": "open_file needs path"}
    root = resolve_root(a.get("_root") or a.get("workspace_root"))
    blocked = blocked_reason(root, a["path"])
    if blocked:
        return {"ok": False, "error": blocked}
    try:
        ap = str(safe_path(root, a["path"]))
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    args = {"filePath": ap}
    if a.get("line") is not None:
        args["line"] = int(a["line"])
    return {"ok": True, "result": JH.call_json("open_file", args)}


def t_get_workspace_info(a) -> dict:
    if JH.is_up():
        try:
            return {"ok": True, "result": JH.call_json("get_workspace_info", {})}
        except Exception as e:
            err(f"[get_workspace_info] vscode failed: {e}")
    root = resolve_root(a.get("_root") or a.get("workspace_root"))
    return {"ok": True, "_via": "local",
            "result": {"folders": [{"name": root.name, "path": str(root)}],
                       "name": root.name, "rootPath": str(root)}}


def t_vscode_status(a) -> dict:
    up = JH.is_up()
    info = None
    if up:
        try:
            info = JH.call_json("get_workspace_info", {}, timeout=10)
        except Exception as e:
            info = {"error": str(e)}
    folders = []
    if isinstance(info, dict):
        folders = info.get("folders") or info.get("workspaceFolders") or []
    root = resolve_root(a.get("_root") or a.get("workspace_root"))
    app = bool(up)
    place = bool(up and folders)
    return {"ok": True, "app": app, "place": place, "root": str(root), "workspace": info}


TOOLS = [
    ("read_file", "Read file contents (supports line ranges). Paths are relative to the VSCode workspace root. "
     "Secret-looking files (.env, *.pem, *secret*...) are refused; likely secrets in output are auto-redacted.",
     {"path": "relative file path", "start_line": 1, "max_lines": 400}, t_read_file),
    ("write_file", "Write content to a file (integrates with VSCode undo when the MCP server is on). "
     "Secret-looking files are refused - never write credentials with this tool.",
     {"path": "relative file path", "content": "full new content"}, t_write_file),
    ("list_files", "List workspace files by glob pattern. Secret-looking files are hidden from results.",
     {"pattern": "**/*.py", "max_results": 200}, t_list_files),
    ("grep", "Regex search across workspace files. Secret-looking files are skipped; likely secrets in hits are auto-redacted.",
     {"query": "regex", "include": "*", "max_results": 50}, t_grep),
    ("run_terminal", "Run a shell command in the workspace and capture output.",
     {"command": "shell command", "cwd": ".", "timeout_sec": 30}, t_run_terminal),
    ("get_active_file", "Current file open in VSCode: path, full content, language, cursor. Needs the MCP server.",
     {}, t_get_active_file),
    ("get_diagnostics", "LSP errors/warnings from VSCode language servers. Needs the MCP server.",
     {"filePath": "absolute path or omit", "severity": "error|warning|information|hint"}, t_get_diagnostics),
    ("show_diff", "Preview a change in VSCode's native diff editor BEFORE writing. Needs the MCP server.",
     {"path": "relative file path", "content": "proposed full content", "title": "tab title"}, t_show_diff),
    ("open_file", "Open a file in the VSCode editor, optionally at a line. Needs the MCP server.",
     {"path": "relative file path", "line": 0}, t_open_file),
    ("get_workspace_info", "VSCode workspace root, name and folders.",
     {}, t_get_workspace_info),
    ("vscode_status", "Bridge-side VSCode link health: MCP server up? workspace open? root?",
     {}, t_vscode_status),
]


def schema_for(defaults: dict) -> dict:
    props = {}
    for k, d in defaults.items():
        t = "string"
        if isinstance(d, bool):
            t = "boolean"
        elif isinstance(d, (int, float)) and not isinstance(d, bool):
            t = "number"
        props[k] = {"type": t, "description": str(d) if not isinstance(d, str) else d}
    return {"type": "object", "properties": props}


TOOL_DEFS = [{"name": n, "description": d, "inputSchema": schema_for(s)} for n, d, s, _ in TOOLS]
TOOL_FN = {n: fn for n, _, _, fn in TOOLS}


AUDIT_PATH = Path(__file__).resolve().parent.parent / "logs" / "vb_audit.jsonl"


def audit(tool: str, args: dict, out) -> None:
    """Append-only local audit trail: what was sent toward the AI."""
    try:
        import datetime
        if isinstance(out, dict):
            text = json.dumps(out, ensure_ascii=False)
            redacted = out.get("redacted", 0)
            ok = out.get("ok", True)
        else:
            text = str(out)
            redacted = 0
            ok = True
        rec = {
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "tool": tool,
            "target": str(args.get("path") or args.get("query")
                          or args.get("command") or args.get("filePath") or "")[:200],
            "ok": ok,
            "bytes_out": len(text.encode("utf-8")),
            "redacted": redacted,
            "via": out.get("_via") if isinstance(out, dict) else None,
        }
        AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(AUDIT_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        err(f"audit failed: {e}")


def reply(rid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": rid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result if result is not None else {}
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        method = msg.get("method")
        rid = msg.get("id")
        params = msg.get("params") or {}
        try:
            if method == "initialize":
                reply(rid, {"protocolVersion": "2024-11-05", "capabilities": {},
                            "serverInfo": {"name": "vibebridge-vscode", "version": VERSION}})
            elif method == "notifications/initialized":
                continue
            elif method == "tools/list":
                reply(rid, {"tools": TOOL_DEFS})
            elif method == "tools/call":
                name = params.get("name", "")
                args = params.get("arguments") or {}
                fn = TOOL_FN.get(name)
                if fn is None:
                    reply(rid, error={"code": -32602, "message": f"unknown tool '{name}'"})
                    continue
                try:
                    out = fn(args)
                    # Bridge-internal health probes are not AI-bound traffic.
                    if name != "vscode_status":
                        audit(name, args, out)
                except Exception as e:
                    reply(rid, error={"code": -32603, "message": f"{type(e).__name__}: {e}"})
                    continue
                reply(rid, {"content": [{"type": "text",
                                        "text": out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)}]})
            elif rid is not None:
                reply(rid, error={"code": -32601, "message": f"unknown method '{method}'"})
        except BrokenPipeError:
            break
        except Exception as e:
            err(f"fatal: {e}")
            if rid is not None:
                try:
                    reply(rid, error={"code": -32603, "message": str(e)})
                except Exception:
                    break


if __name__ == "__main__":
    main()
