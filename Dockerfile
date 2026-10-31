FROM python:3.12-slim

LABEL io.modelcontextprotocol.server.name="io.github.contentforge-press/feeguard-mcp"

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY feeguard_mcp ./feeguard_mcp

# 无依赖，不装任何东西。镜像里只有标准库和这两个文件。
ENTRYPOINT ["python3", "-m", "feeguard_mcp"]
