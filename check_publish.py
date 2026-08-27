"""发布前密钥扫描门禁（check_publish gate）。

0.1.0 事故后强制流程的最后一道闸：对 dist/ 下所有产物做
  (a) 文件名白名单核对（绝不出现 .env/workspace/.history/.memory/密钥文件）
  (b) 内容级密钥模式扫描（API key 格式 + 真实 .env 值子串，值只读取不回显）
任何命中 → exit 1，禁止上传。

用法（在 src/ 仓库根执行）：
    python -m build
    python check_publish.py          # 非零 = DO NOT upload
    python -m twine check dist/*     # 通过后再上传

依赖：标准库 only（zipfile/tarfile/re 扫描 wheel 与 sdist）。
"""

from __future__ import annotations

import re
import sys
import tarfile
import zipfile
from pathlib import Path

DIST_DIR = Path(__file__).resolve().parent / "dist"

# 密钥形态模式（0.1.0 事故时 PyPI 检测到的同类 token 都在此列）
_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("pypi-token", re.compile(r"pypi-AgEIc[A-Za-z0-9_-]{40,}")),
    ("openai-key", re.compile(r"sk-[A-Za-z0-9]{32,}")),
    ("google-key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("aws-key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("github-token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36}")),
    ("slack-token", re.compile(r"xox[bpras]-[0-9A-Za-z-]{10,}")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("private-key", re.compile(r"-----BEGIN (RSA |DSA |EC |OPENSSH )?PRIVATE KEY-----")),
]

# 文件名黑名单（相对路径包含任一即判失败）
_BAD_NAME_SUBSTRINGS = (".env", "workspace/", ".history", ".memory", ".logs",
                        "config.user.yaml", "config.runtime.yaml", "*.pem", "*.key")

# .env 位置：仓库根上一级（open-ant/），值只用于子串比对、绝不打印
_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

# 非机密配置标识：公开信息（模型名/集合名/实例 id），合法出现在代码默认值里
_NON_SECRET_NAMES = {
    "EMBED_MODEL_NAME", "EMBED_MODEL_TYPE", "LLM_MODEL_ID",
    "QDRANT_COLLECTION", "QDRANT_DISTANCE", "AURA_INSTANCEID",
    "AURA_INSTANCENAME", "NEO4J_DATABASE", "MYSQL_DATABASE",
}

# 变量名含这些词根的一律视为机密值参与扫描
_SECRET_NAME_MARKERS = ("PASSWORD", "KEY", "TOKEN", "URI", "DSN", "SECRET")


def _read_env_values() -> list[str]:
    """Read non-empty .env values whose variable name is secret-shaped.

    显式非机密配置标识（模型名/集合名/实例 id 等公开信息）不参与扫描——
    它们合法地出现在 config 默认值、loader fallback 与测试断言里。
    任何含 PASSWORD/KEY/TOKEN/URI/DSN 的变量名一律视为机密参与扫描。
    """
    if not _ENV_PATH.exists():
        return []
    values = []
    for line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name, value = name.strip(), value.strip()
        if len(value) < 10:
            # 太短的值（guest、1、空）没有辨识度，跳过避免误报
            continue
        if name in _NON_SECRET_NAMES:
            continue
        if any(marker in name.upper() for marker in _SECRET_NAME_MARKERS):
            values.append(value)
    return values


def _scan_text(name: str, text: str, env_values: list[str]) -> list[str]:
    hits = []
    for pattern_name, pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            hits.append(f"{name}: 命中密钥模式 {pattern_name}")
    for value in env_values:
        if value in text:
            hits.append(f"{name}: 含 .env 真实值子串（长度 {len(value)}）")
    return hits


def main() -> int:
    artifacts = sorted(DIST_DIR.glob("*"))
    if not artifacts:
        print("FAIL: dist/ 为空——先运行 python -m build")
        return 1

    env_values = _read_env_values()
    problems: list[str] = []
    scanned = 0

    for artifact in artifacts:
        name = artifact.name
        # (a) 文件名核对
        if not (name.endswith(".whl") or name.endswith(".tar.gz")):
            problems.append(f"{name}: 非 wheel/sdist 产物")
            continue
        # 只检查当前版本产物（避免历史版本残留误报）
        low = name.lower()
        if ".dev" in low or "+" in low:
            continue

        scanned += 1
        # (b) 内容扫描
        if name.endswith(".whl"):
            with zipfile.ZipFile(artifact) as zf:
                for member in zf.namelist():
                    if any(bad in member for bad in _BAD_NAME_SUBSTRINGS):
                        problems.append(f"{name}: 文件名黑名单命中 {member}")
                    try:
                        content = zf.read(member).decode("utf-8", errors="ignore")
                    except Exception:
                        continue
                    problems.extend(_scan_text(f"{name}::{member}", content, env_values))
        else:
            with tarfile.open(artifact, "r:gz") as tf:
                for member in tf.getmembers():
                    if not member.isfile():
                        continue
                    if any(bad in member.name for bad in _BAD_NAME_SUBSTRINGS):
                        problems.append(f"{name}: 文件名黑名单命中 {member.name}")
                    try:
                        content = tf.extractfile(member)
                        text = content.read().decode("utf-8", errors="ignore") if content else ""
                    except Exception:
                        continue
                    problems.extend(_scan_text(f"{name}::{member.name}", text, env_values))

    if problems:
        print("FAIL —— 禁止上传（0.1.0 密钥泄露事故的门禁）：")
        for p in problems:
            print(f"  - {p}")
        return 1

    print(f"PASS: 扫描 {scanned} 个产物（{len(artifacts)} 个文件），"
          f"无密钥模式命中、无 .env 值泄露、无黑名单文件名")
    return 0


if __name__ == "__main__":
    sys.exit(main())
