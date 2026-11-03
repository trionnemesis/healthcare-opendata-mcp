# hcmcp — 同一 image 雙 entrypoint:`hcmcp`(server,預設)/ `hcmcp-sync`(ETL)
FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && useradd --uid 10001 --create-home hcmcp \
    && mkdir -p /data && chown 10001 /data

USER 10001

# GKE 預設:streamable HTTP + DB 掛載於 /data(emptyDir / volume)
# FASTMCP_SHOW_SERVER_BANNER=false:banner 會順便向 pypi.org 查最新版本
# (fastmcp/utilities/cli.py → check_for_newer_version)並寫
# $HOME/.local/share/fastmcp/version_cache.json。正式 image 不需要這個檢查,
# 關掉可省去每次啟動對第三方主機的 egress,也讓 readOnlyRootFilesystem
# 成為無寫入需求的設定(見 deploy/k8s/deployment.yaml)。
ENV HCMCP_TRANSPORT=http \
    HCMCP_DB=/data/hcmcp.db \
    HCMCP_PORT=8000 \
    FASTMCP_SHOW_SERVER_BANNER=false

EXPOSE 8000
ENTRYPOINT ["hcmcp"]
