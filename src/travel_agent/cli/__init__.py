"""`journeypilot` 维护命令行。

入口是仓库根的 `journeypilot.py`（`uv run python journeypilot.py …`）。
这个包不注册 console script：`[tool.uv] package = false` 意味着这个仓不作为包安装，
声明一个装不上的 entry point 只会让文档里的命令敲不出来。
"""
