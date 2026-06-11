# GKE 部署指南

## 架構

```
CronJob hcmcp-sync(每日 02:30)
  init: hcmcp-sync ──▶ /data/hcmcp.db(emptyDir)
  init: gcloud storage cp ──▶ gs://HCMCP_BUCKET/hcmcp.db
  main: kubectl rollout restart deployment/hcmcp
                                        │
Deployment hcmcp(replicas: 2,唯讀)     ▼
  init: gcloud storage cp gs://HCMCP_BUCKET/hcmcp.db ──▶ emptyDir
  main: hcmcp(HCMCP_TRANSPORT=http,/healthz probe)
                │
Service hcmcp(ClusterIP :8000)──▶ Internal LB / IAP(自行配置)
```

設計原則:SQLite 為**不可變唯讀 artifact**(~200MB),每 pod 自帶一份
emptyDir 副本 → 無鎖競爭、replica 自由擴展、不需 PVC / Cloud SQL。

## 前置作業

1. **GCS bucket**:建立 `gs://HCMCP_BUCKET`(替換所有 manifest 中的 `HCMCP_BUCKET`)
2. **Image**:`docker build -t REGISTRY/hcmcp:latest . && docker push ...`
   (替換 manifest 中的 `REGISTRY/hcmcp:latest`)
3. **Workload Identity**:
   - GSA `hcmcp-reader`:bucket `roles/storage.objectViewer` → 綁 KSA `hcmcp`
   - GSA `hcmcp-writer`:bucket `roles/storage.objectAdmin` → 綁 KSA `hcmcp-sync`
   - 取消 `rbac.yaml` 中 annotation 註解並填入 GSA

## 部署

```bash
kubectl apply -f deploy/k8s/

# 首次部署 bucket 尚無 DB,手動觸發一次 sync(否則 server initContainer 會失敗重試)
kubectl create job --from=cronjob/hcmcp-sync hcmcp-bootstrap
kubectl logs job/hcmcp-bootstrap -f
```

## 驗證

```bash
kubectl port-forward svc/hcmcp 8000:8000
curl http://localhost:8000/healthz          # {"status":"ok"}
# MCP endpoint(streamable HTTP):http://localhost:8000/mcp
claude mcp add --transport http hcmcp http://localhost:8000/mcp
```

## 注意事項

- **認證**:MCP server 本身無認證,僅限叢集內部 / Internal LB 存取;
  需跨網段存取時前面掛 IAP 或 service mesh mTLS
- **SSE 已 deprecated**(MCP spec 2025-03-26):`HCMCP_TRANSPORT=sse` 僅保留
  給既有部署,GKE 一律用 `http`(stateless,LB 不需 sticky session)
- **資料更新即生效**:sync CronJob 最後一步 rollout restart;
  若不想重啟,也可改用 Reloader 監看 ConfigMap 版本戳記
