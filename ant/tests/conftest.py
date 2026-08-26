# 共享测试配置。
# pytest-asyncio 已通过根 pyproject.toml 的
# [tool.pytest.ini_options] asyncio_mode = "auto" 全局启用，
# 各测试文件无需再手动标注 @pytest.mark.asyncio。
# 注意：本文件刻意不 import ant，避免纯工具测试触发重量级依赖（litellm 等）。
