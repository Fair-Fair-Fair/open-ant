"""DefLoader/AgentLoader UTF-8 回归测试（Phase 7 记忆方舟）。

中文 agent/skill/cron 定义是中文产品的核心路径，但 Windows（cp936 locale）
下 ``read_text()`` 默认编码会乱码或崩溃——记忆方舟接入中文 persona
（xiaoan/shu）时发现并修复，本文件锁定该修复。
"""

from pathlib import Path

from ant.core.agent_loader import AgentLoader
from ant.utils.config import Config, LLMConfig
from ant.utils.def_loader import (
    discover_definitions,
    parse_definition,
    write_definition,
)

CHINESE_BODY = "你是小安，一位温和的陪伴助手。\n请永远用中文、用短句回复。"
CHINESE_NAME = "小安"


def _make_config(tmp_path: Path) -> Config:
    return Config(
        workspace=tmp_path,
        llm=LLMConfig(provider="openai", model="test-model", api_key="test-key"),
        default_agent="xiaoan",
    )


def test_parse_definition_chinese_body() -> None:
    content = f"---\nname: {CHINESE_NAME}\n---\n\n{CHINESE_BODY}"
    frontmatter, body = parse_definition(
        content, "xiaoan", lambda i, fm, b: (fm, b)
    )
    assert frontmatter["name"] == CHINESE_NAME
    assert CHINESE_BODY in body


def test_write_and_discover_chinese_definition(tmp_path: Path) -> None:
    base = tmp_path / "agents"
    write_definition(
        "xiaoan",
        {"name": CHINESE_NAME, "description": "温和陪伴"},
        CHINESE_BODY,
        base,
        "AGENT.md",
    )
    found = discover_definitions(base, "AGENT.md", lambda i, fm, b: (i, fm, b))
    assert len(found) == 1
    agent_id, frontmatter, body = found[0]
    assert agent_id == "xiaoan"
    assert frontmatter["name"] == CHINESE_NAME
    assert CHINESE_BODY in body


def test_agent_loader_chinese_agent(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    agent_dir = config.agents_path / "xiaoan"
    agent_dir.mkdir(parents=True)
    (agent_dir / "AGENT.md").write_text(
        f"---\nname: {CHINESE_NAME}\ndescription: 温和陪伴\n---\n\n{CHINESE_BODY}",
        encoding="utf-8",
    )
    (agent_dir / "SOUL.md").write_text(
        "人格：温和、耐心、永远用中文。", encoding="utf-8"
    )
    agent_def = AgentLoader(config).load("xiaoan")
    assert agent_def.name == CHINESE_NAME
    assert CHINESE_BODY in agent_def.agent_md
    assert "人格" in agent_def.soul_md
