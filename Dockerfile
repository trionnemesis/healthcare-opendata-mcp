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
# HCMCP_HOST 在此明確設定:程式碼預設為 127.0.0.1(本機安全),容器內需綁
# 0.0.0.0 才能讓 kubelet probe 與 Service 連到 pod。
ENV HCMCP_TRANSPORT=http \
    HCMCP_DB=/data/hcmcp.db \
    HCMCP_HOST=0.0.0.0 \
    HCMCP_PORT=8000

EXPOSE 8000
ENTRYPOINT ["hcmcp"]
