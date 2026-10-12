# QMS Ye · 成品仓 WMS

基于 InvenTree 1.5.1 二次开发的仓库管理系统，面向设备/成品的入库、出库、审批、项目归属、合同质保和标签打印。仓库包含应用源码、容器部署配置及中文业务文档，可从一次普通 Git clone 开始构建。

## 当前功能

- **扫码与库存**：识别设备编号、InvenTree 库存码和完整明细标签；扫码后先填充核对信息，由操作员确认数量并加入申请。重复扫描完整标签只定位原明细，不按扫描次数累加数量。
- **整单审批**：入库审批前不落库存；出库加入草稿后冻结数量，通过后原子扣减，驳回、撤销或超时释放冻结。支持库位审批负责人、禁止自审和操作权限。
- **项目、合同与质保**：统一项目档案、合同管理、入库准备单、来源明细追溯、合同验收日期与质保月数、历史库存归属补录。
- **标签与打印**：设备 IPN 标签、项目/合同完整标签、每条设备独立打印份数、PDF 下载，以及可选的 Windows QZ Tray 本地打印。
- **审计与通知**：扫码事件、原生库存流水、审批时间线、幂等重试、冻结/可用库存，以及可选的企业微信群机器人通知。
- **中文工作台**：按操作员、审批人及管理员权限展示工作入口，保留 InvenTree 原生库存与基础资料管理能力。

当前扫码页面的摄像头入口暂时停用；Windows 打印机、驱动、实物出纸和打印后扫码验收尚未完成。本地队列提交成功不等于已经出纸。完整业务流程见[合同、质保及完整标签使用说明](docs/INVENTREE_WMS_CONTRACT_WARRANTY_GUIDE.md)。

## 架构与目录

浏览器中的 React / TypeScript 工作台通过 Django REST API 访问业务服务；PostgreSQL 17 保存库存和审批数据，Redis 7 提供缓存，Django-Q2 worker 处理异步任务，Caddy 提供 HTTPS 和静态资源。

```text
qms-ye/
├── InvenTree/          # Django 后端、React 前端、数据库迁移与测试
├── inventree-deploy/   # Compose、Caddy、环境变量模板与运维手册
└── docs/               # 二开方案、操作指南和历史验收记录
```

`InvenTree/` 是本仓库中的普通源码目录，不需要初始化子模块。数据库、上传文件、真实环境变量、私钥、备份、依赖目录和本地验收日志不进入版本控制。

## 首次运行

需要 Git、Docker Engine / Docker Desktop（含 Compose v2）、Node.js 24 和 Corepack。先在本机配置能访问目标仓库的 SSH key。

```bash
git clone git@github.com:junfengye02-spec/qms-ye.git
cd qms-ye
cp inventree-deploy/.env.example inventree-deploy/.env
```

编辑 `.env`：填写数据库密码，把站点地址改成部署机可访问的域名或局域网 IP；保留 `INVENTREE_IMAGE=wmsye-inventree:dev`，以运行本仓库构建的二开镜像。

```bash
# 从源码构建前端和应用镜像
bash InvenTree/contrib/container/build-wms.sh

cd inventree-deploy
mkdir -p inventree-data
chmod 600 .env
docker compose config --quiet
docker compose up -d inventree-db inventree-cache

# 仅用于首次创建空数据库；已有环境升级请看部署手册
docker compose run --rm inventree-server invoke update --skip-backup
docker compose run --rm inventree-server invoke superuser
docker compose up -d
docker compose ps
```

管理员账号在上述交互命令中自行创建，不提供默认密码。浏览器访问 `.env` 中的 `INVENTREE_SITE_URL`。Caddy 默认使用内部 CA，首次访问须按[部署手册](inventree-deploy/README.md)安装可信根证书。登录后建立库位、商品、项目、合同，并配置业务用户权限和库位审批负责人。

## 开发与验证

```bash
cd InvenTree/src/frontend
corepack yarn install --frozen-lockfile
node --experimental-transform-types --test tests/unit/*.test.ts
corepack yarn compile
corepack yarn build
```

后端库存、审批、合同、项目和打印测试应运行在独立测试数据库；命令与隔离要求见[开发说明](docs/DEVELOPMENT.md)。端到端测试会创建数据，需单独准备测试环境。

## 文档

- [部署、升级、备份与环境变量](inventree-deploy/README.md)
- [开发结构与测试命令](docs/DEVELOPMENT.md)
- [首次导入验证记录](docs/INITIAL_IMPORT_VERIFICATION.md) / [变更记录](CHANGELOG.md)
- [合同、质保及完整标签使用说明](docs/INVENTREE_WMS_CONTRACT_WARRANTY_GUIDE.md)
- [Windows 本地打印配置](inventree-deploy/WINDOWS_LOCAL_PRINT.md)
- [Windows 实机验收交接](docs/WINDOWS_LOCAL_PRINT_READY_FOR_WINDOWS.md)
- [基础库存需求](docs/INVENTREE_WMS_INVENTORY_REQUIREMENTS.md)
- [二次开发功能清单（历史盘点）](docs/INVENTREE_WMS_SECONDARY_DEVELOPMENT_FEATURE_LIST.md)
- [库存历史验收](docs/INVENTREE_WMS_INVENTORY_ACCEPTANCE.md) / [合同质保历史验收](docs/INVENTREE_WMS_CONTRACT_WARRANTY_ACCEPTANCE.md)

方案与验收文档保留各阶段背景，旧地址、数量及运行状态不代表新部署结果；以当前源码和本 README 的启动流程为准。文档提及的本地日志、截图和运行数据未随仓库上传。

## 上游与许可

源码基于 InvenTree **1.5.1**，上游基线 commit 为 `84052e1ac8087c2ede499d72310c67e74dad3017`。二开镜像的上游底座 digest 固定在 `InvenTree/contrib/container/Dockerfile.wms`。

保留 InvenTree Developers 的版权声明及 [MIT 许可证](InvenTree/LICENSE)。本项目为独立二次开发版本；第三方依赖遵循其各自许可证，前端依赖及版本见 [package.json](InvenTree/src/frontend/package.json)。
