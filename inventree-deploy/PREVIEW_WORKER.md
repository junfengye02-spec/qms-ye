# 18000 预览环境后台任务

标签打印生成 PDF 依赖 Django Q 后台任务。预览环境使用数据库
`wms_inventory_acceptance`，正式服务使用 `inventree`；正式 worker 无法消费预览数据库的队列。

2026-09-09 排查发现：预览环境没有 worker，但与正式环境共用 Redis DB 0。
`is_worker_running()` 读到了正式 worker 的状态，将任务错误地留在预览队列中。
打印输出 1 因此停在 `0 / 1`，当时队列共 493 项。

修复后的配置：

- `wms-preview-proxy`：承接 18000 端口和局域网访问；通过 `/auth/` 校验登录后提供媒体文件。
- `wms-preview`：内部 8000 端口，保留原数据库和挂载；Redis DB 1。
- `wms-preview-worker`：与预览 web 使用同一镜像、源码挂载、数据库及 Redis DB 1，运行 `python manage.py qcluster`。
- 三个容器均设置 `unless-stopped` 自动重启。正式服务继续使用 Redis DB 0。

部署脚本从现有预览容器读取配置，不在文件中保存数据库密码：

```sh
python3 inventree-deploy/scripts/enable-preview-worker.py
```

脚本保留旧 web 容器（`wms-preview-before-worker-时间戳`）供恢复，不删除数据库。
后续重建预览环境时必须同时保留独立 worker 和 `INVENTREE_CACHE_DB=1`。
仅启动 `runserver` 不能代替后台服务。

检查：

```sh
docker ps --filter name=wms-preview
docker logs --tail 30 wms-preview-worker
```

页面打印使用 `inventreelabel` 时生成 PDF，由浏览器打开文件后再选择实际打印机。

同时修复了 PDF 下载入口：预览原来直接暴露 `runserver`，且 `DEBUG=False`，
`/media/` 不会由 Django 提供，落入兜底跳转到首页。现在用独立 Caddy 代理
提供媒体文件（只读挂载，并保留身份验证），没有开启 DEBUG 或修改业务代码。

验收：原任务 1 和操作员新提交的任务 2 均完成 `1 / 1`，无生成错误。
新 PDF 可解析为 1 页，内容含设备名称及设备 IPN；库存记录仍为 236 条。
正式 worker 与预览 worker 的状态分别来自各自 Redis 数据库。

浏览器验收：操作员正常登录后下载 PDF 返回 HTTP 200（99,279 字节）；未登录访问同一媒体入口被拒绝。
