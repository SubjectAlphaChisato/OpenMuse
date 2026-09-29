"""Shared helpers for the release scripts: locating uv, node, and npm on this or any machine."""
from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "server"
WEB = ROOT / "web"
LINGCODE_NODE = Path("/Applications/LingCode.app/Contents/Resources/agent-bridge/node")
STANDALONE_NPM = Path.home() / ".local" / "npm-standalone" / "package" / "bin" / "npm-cli.js"


def tool_path() -> str:
    """A PATH with uv, node, and npm. Falls back to LingCode's bundled Node and a standalone npm when they are not installed."""
    extra: list[str] = [str(Path.home() / ".local" / "bin")]
    have_node, have_npm = shutil.which("node"), shutil.which("npm")
    if (not have_node or not have_npm) and LINGCODE_NODE.exists():
        shim = Path(tempfile.mkdtemp(prefix="opa-node-shim-"))
        node_bin = have_node or str(LINGCODE_NODE)
        if not have_node:
            (shim / "node").symlink_to(LINGCODE_NODE)
        if not have_npm and STANDALONE_NPM.exists():
            npm = shim / "npm"
            npm.write_text(f'#!/bin/sh\nexec "{node_bin}" "{STANDALONE_NPM}" "$@"\n')
            npm.chmod(npm.stat().st_mode | stat.S_IEXEC)
        extra.append(str(shim))
    return os.pathsep.join([*extra, os.environ.get("PATH", "")])


def node() -> str:
    return shutil.which("node", path=tool_path()) or str(LINGCODE_NODE)
