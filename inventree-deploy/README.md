# 部署与运维

本目录通过 Docker Compose 运行 QMS Ye 成品仓 WMS：自定义 InvenTree Web、异步 worker、PostgreSQL 17、Redis 7 和 Caddy。源代码位于同级 `InvenTree/`，基础镜像版本为 InvenTree 1.5.1。

## 首次部署

安装 Docker（含 Compose v2）、Node.js 24 和 Corepack。从仓库根目录执行：

```bash
cp inventree-deploy/.env.example inventree-deploy/.env
```

编辑 `.env`，至少填写非空数据库密码，并将 `INVENTREE_SITE_HOST` 与 `INVENTREE_SITE_URL` 改为实际地址。`localhost` 只适合部署机本身访问；局域网终端使用部署机 IP 或可解析的域名。

```bash
# 编译语言包、前端，生成 wmsye-inventree:dev
bash InvenTree/contrib/container/build-wms.sh

cd inventree-deploy
mkdir -p inventree-data
chmod 600 .env
docker compose config --quiet
docker compose up -d inventree-db inventree-cache

# 首次空库初始化；跳过尚不存在的数据备份
docker compose run --rm inventree-server invoke update --skip-backup
docker compose run --rm inventree-server invoke superuser
docker compose up -d
docker compose ps
```

`invoke superuser` 交互创建管理员。不要将账号密码写进 README 或提交 `.env`。启动后建立库位、商品、项目及合同，分配操作和审批权限，并配置库位审批 Owner。是否启用扫码审批及其时限由系统业务设置控制，不由示例环境变量自动创建。

构建脚本按 `yarn.lock` 安装依赖，并要求前端构建产物包含 `ScanStockPage.tsx`。正常构建只编译现有翻译；需要提取新文案时使用 `WMS_EXTRACT_TRANSLATIONS=1 bash InvenTree/contrib/container/build-wms.sh`。不要使用官方 `stable` 镜像代替二开镜像，否则不会获得本项目功能。

## 环境变量

完整起点为 [.env.example](.env.example)。示例密码留空，Compose 会拒绝空数据库密码。

| 变量 | 用途 |
| --- | --- |
| `INVENTREE_IMAGE` | 本地构建镜像，默认示例为 `wmsye-inventree:dev` |
| `INVENTREE_EXT_VOLUME` | 持久化路径，示例为 `./inventree-data` |
| `INVENTREE_DB_ENGINE/HOST/PORT/NAME/USER/PASSWORD` | PostgreSQL 连接；容器主机为 `inventree-db` |
| `INVENTREE_CACHE_ENABLED/HOST/PORT/DB` | Redis 缓存；容器主机为 `inventree-cache` |
| `INVENTREE_WEB_PORT` | 应用内部端口，当前配置使用 `8000` |
| `INVENTREE_SITE_HOST` | Caddy 站点主机名或 IP，不带协议、路径 |
| `INVENTREE_SITE_URL` | 外部完整 HTTPS 地址，与站点主机匹配 |
| `INVENTREE_HTTP_PORT/HTTPS_PORT` | 宿主机端口，示例为 `8088` / `443` |
| `INVENTREE_TIMEZONE` | 应用时区，示例为 `Asia/Shanghai` |
| `INVENTREE_WECOM_BOT_WEBHOOK_URL` | 可选，企业微信群机器人地址 |
| `INVENTREE_WECOM_BOT_MENTION_USER_IDS` | 可选，提交审批时提醒的 userId，逗号分隔 |
| `INVENTREE_LOCAL_PRINT_*` | 可选，本地打印开关、版本和下载地址 |

Caddy 的 HTTP 跳转默认指向 HTTPS 443；修改外部 HTTPS 端口时需要同时调整 Caddy 跳转目标。Compose 明确使用固定容器名，在同一 Docker 主机部署第二套环境时应另行隔离容器名、端口、数据库及数据目录。

## HTTPS 与局域网访问

Caddy 默认以内部 CA 签发证书，并把 HTTP 跳转到 HTTPS。PostgreSQL、Redis 和应用端口不发布到宿主机；对外发布的 HTTP/HTTPS 端口绑定 `0.0.0.0`。

首次启动后，可从当前代理容器导出**公钥根证书**：

```bash
docker compose cp inventree-proxy:/data/caddy/pki/authorities/local/root.crt ./inventree-local-ca.crt
```

将这张根证书导入需要访问本站的终端信任库；不要分发 CA 私钥。之后访问 `.env` 中的 HTTPS 地址。更换主机 IP 或域名时，同步更新 `INVENTREE_SITE_HOST` 和 `INVENTREE_SITE_URL`，再运行 `docker compose up -d`。公网证书部署需要另行调整 `Caddyfile` 的 `tls internal` 配置。

## 日常维护与升级

```bash
docker compose ps
docker compose logs --tail=100 inventree-server inventree-worker inventree-proxy
docker compose exec inventree-server invoke int.showmigrations

# 停止服务，不删除绑定的数据目录
docker compose down
```

升级前将数据库、上传媒体、配置与密钥备份到独立受保护的位置，并先在副本验证迁移。运行中的数据位于 `INVENTREE_EXT_VOLUME`；不要直接把运行中的 PostgreSQL 文件复制视为一致性备份。应用的 `invoke backup` 可备份数据库和媒体：

```bash
docker compose exec inventree-server invoke backup
```

完成备份后，从仓库根目录重新构建，再迁移并启动：

```bash
bash InvenTree/contrib/container/build-wms.sh
cd inventree-deploy
docker compose stop inventree-server inventree-worker
docker compose run --rm inventree-server invoke update
docker compose up -d
docker compose ps
```

已有数据库升级不使用 `--skip-backup`。`invoke update` 会在迁移前执行应用备份；仍需将备份复制到独立位置。涉及唯一约束或历史数据回填的迁移如果报告冲突，应核对原数据后再继续，不要跳过迁移或直接合并记录。新应用回退与数据库回退分别处理，不自动逆迁移删除审计记录。

## 扫码与审批

当前工作台支持扫码枪 HID 键盘输入。扫码回车先显示设备、项目、合同、库位及数量核对信息；操作员明确点击加入待入库/待出库，再提交整单审批。完整标签重复扫码定位原行，不按次数累加。请求失败后的安全重试复用原事件 ID 和参数。详细规则见[业务操作指南](../docs/INVENTREE_WMS_CONTRACT_WARRANTY_GUIDE.md)。

## 企业微信群机器人

仅在 `.env` 中填写实际 Webhook，缺省留空。后台 worker 在数据库事务提交后异步通知；发送失败不会回滚库存。支持直接入库成功、申请提交、审批通过/驳回、撤销和超时；草稿逐条扫码不发群消息。发布后用专用测试群验证网络和配置。

## Windows 本地标签打印

基础部署默认关闭本地打印，保留 PDF 使用路径。启用前阅读 [Windows 本地打印手册](WINDOWS_LOCAL_PRINT.md)，准备经过验证的 QZ Tray 版本、HTTPS 下载地址和匹配的签名证书/私钥。可选 overlay：

```bash
docker compose -f docker-compose.yml -f docker-compose.local-print.yml up -d
```

overlay 要求 `WMS_LOCAL_PRINT_CERTIFICATE_FILE` 和 `WMS_LOCAL_PRINT_PRIVATE_KEY_FILE` 指向宿主机绝对路径，私钥只挂载到签名服务，不上传 GitHub。目前软件逻辑及本地模拟验收不代表 Windows 实机、驱动、出纸或实物扫码验收完成。

## 仓库内容边界

仓库只提供配置模板与代码，不包含现有业务数据库、上传媒体、生产 `.env`、证书私钥、备份、运行日志和本机测试产物。`scripts/enable-preview-*.py` 及 `PREVIEW_*.md` 用于已有隔离预览实例，包含该实例假设，不属于新环境安装流程。历史验收文档中的本地路径或日志引用不保证在新 clone 中存在。

更多资料：[开发与测试](../docs/DEVELOPMENT.md)、[部署成本清单](DEPLOYMENT_COST_CHECKLIST.md)。
