"""Release gate: no secret values in the repository (spec section 11).

Scans every file git would ship (tracked plus untracked-but-not-ignored) for real credential formats,
and checks that local secret files (.env, .mcp.json) are ignored and not tracked.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PATTERNS = {
    "private key": re.compile(r"-----BEGIN (RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"),
    "Anthropic API key": re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    "OpenAI-style key": re.compile(r"\bsk-(proj-)?[A-Za-z0-9]{32,}"),
    "AWS access key": re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),
    "Google OAuth client secret": re.compile(r"GOCSPX-[A-Za-z0-9_-]{20,}"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "Google OAuth token": re.compile(r"\bya29\.[0-9A-Za-z_-]{20,}"),
    "GitHub token": re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}\b"),
    "Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    "filled-in secret variable": re.compile(r"^\s*(OPA_ANTHROPIC_API_KEY|OPA_GOOGLE_CLIENT_SECRET|OPA_BROKER_KEK|OPA_INTERNAL_TOKEN)[ \t]*=[ \t]*[^\s#]{8,}", re.M),
}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".ico", ".woff", ".woff2", ".lock"}
MUST_BE_IGNORED = [".env", ".mcp.json"]


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def main() -> int:
    problems: list[str] = []
    files = [f for f in git("ls-files", "-co", "--exclude-standard").splitlines() if f]
    for name in files:
        path = ROOT / name
        if not path.is_file() or path.suffix in SKIP_SUFFIXES or name.endswith("package-lock.json"):
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        for label, pattern in PATTERNS.items():
            for m in pattern.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                problems.append(f"{name}:{line}: {label}")
    tracked = set(git("ls-files").splitlines())
    for name in MUST_BE_IGNORED:
        if name in tracked:
            problems.append(f"{name} is tracked by git; it must never be committed")
        ignored = subprocess.run(["git", "check-ignore", "-q", name], cwd=ROOT).returncode == 0
        if not ignored:
            problems.append(f"{name} is not ignored by .gitignore")
    for p in problems:
        print("SECRET", p)
    print(f"secret scan: {len(files)} files checked, {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
