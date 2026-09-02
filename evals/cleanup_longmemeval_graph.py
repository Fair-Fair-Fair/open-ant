"""Cleanup for graph-on LongMemEval runs (Phase 7).

graph-on 模式把评测记忆写进了共享 Neo4j（实体名带 ``lmeval-<idx>::``
前缀、Memory 节点 source=longmemeval）。跑完评测后执行本脚本清理：

    python -m evals.cleanup_longmemeval_graph

删除范围严格限定在评测命名空间：
  * source = 'longmemeval' 的 :Memory 节点（连带 SUPERSEDES 边）
  * name 以 'lmeval-' 开头的 :Entity 节点（评测实例命名空间实体）

用户自有数据（非 lmeval- 前缀、非 longmemeval source）不会被触碰。
凭据从 .env 读取（NEO4J_*），值绝不打印。
"""

from __future__ import annotations

import asyncio
import sys

DELETE_MEMORIES = """
MATCH (m:Memory)
WHERE m.source = 'longmemeval'
DETACH DELETE m
RETURN count(m) AS deleted
"""

DELETE_ENTITIES = """
MATCH (e:Entity)
WHERE e.name STARTS WITH 'lmeval-'
DELETE e
RETURN count(e) AS deleted
"""


async def _cleanup() -> tuple[int, int]:
    from neo4j import AsyncGraphDatabase

    from ant.utils.settings import InfraSettings

    infra = InfraSettings()
    uri, user, password = infra.neo4j_uri(), infra.neo4j_username(), infra.neo4j_password()
    if not (uri and user and password):
        print("ERROR: NEO4J_URI/USERNAME/PASSWORD 未在 .env 配置")
        return -1, -1

    driver = AsyncGraphDatabase.driver(uri, auth=(user, password))
    try:
        async with driver.session(database=infra.neo4j_database()) as session:
            mem = [r async for r in await session.run(DELETE_MEMORIES)]
            ent = [r async for r in await session.run(DELETE_ENTITIES)]
        return (
            int(mem[0].get("deleted") or 0) if mem else 0,
            int(ent[0].get("deleted") or 0) if ent else 0,
        )
    finally:
        await driver.close()


def main() -> int:
    mem, ent = asyncio.run(_cleanup())
    if mem < 0:
        return 5
    print(f"已清理: {mem} 个评测 Memory 节点, {ent} 个评测命名空间 Entity 节点")
    return 0


if __name__ == "__main__":
    sys.exit(main())
