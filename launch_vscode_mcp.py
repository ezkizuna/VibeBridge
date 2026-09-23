# SPDX-License-Identifier: GPL-3.0-or-later
# launch_vscode_mcp.py
# ──────────────────────────────────────────────────────────────────────────
#  Launcher for the VibeBridge VSCode MCP server (tools/vscode_mcp_server.py).
#
#  This is the VSCode counterpart of ZeroScript's launch_studio_mcp.py: the
#  bridge spawns this, which execs the real server with the same Python
#  interpreter, transparently forwarding stdio and any CLI args. An explicit
#  override path is supported via `VB_MCP_SERVER_PATH`.
# ──────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

ENV_OVERRIDE = "VB_MCP_SERVER_PATH"


def find_server() -> Optional[Path]:
    override_value = os.environ.get(ENV_OVERRIDE)
    if override_value:
        p = Path(override_value).expanduser()
        if p.is_file():
            return p
        sys.stderr.write(
            f"launch_vscode_mcp: {ENV_OVERRIDE} is set but does not point to a file: {override_value}\n"
        )
    here = Path(__file__).resolve().parent
    candidate = here / "tools" / "vscode_mcp_server.py"
    if candidate.is_file():
        return candidate
    return None


def main() -> int:
    srv = find_server()
    if not srv:
        sys.stderr.write(
            "launch_vscode_mcp: tools/vscode_mcp_server.py not found next to the bridge.\n"
        )
        return 1
    sys.stderr.write(f"launch_vscode_mcp: using {srv}\n")
    sys.stderr.flush()
    proc = subprocess.Popen([sys.executable, str(srv)] + sys.argv[1:])
    try:
        return proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        return proc.wait()


if __name__ == "__main__":
    sys.exit(main())
