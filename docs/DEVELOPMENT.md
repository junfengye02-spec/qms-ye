# 开发与验证

应用源码在 `InvenTree/`，上游基线为 InvenTree 1.5.1 / `84052e1ac8087c2ede499d72310c67e74dad3017`。这是根仓库中的普通目录。上游开发指导见 [CONTRIBUTING.md](../InvenTree/CONTRIBUTING.md)，实际工具配置以当前源码为准。

## 主要代码位置

| 目录 | 内容 |
| --- | --- |
| `InvenTree/src/backend/InvenTree/stock/` | 库存、扫码、审批、冻结、项目与合同质保 |
| `InvenTree/src/backend/InvenTree/part/` | 商品、IPN 与条码约束 |
| `InvenTree/src/backend/InvenTree/report/` | 标签、PDF、本地打印任务与签名 |
| `InvenTree/src/backend/InvenTree/users/` | 业务权限 |
| `InvenTree/src/frontend/src/` | React 页面、表单、组件及业务函数 |
| `InvenTree/src/frontend/tests/` | Node 单元测试及 Playwright 测试 |
| `InvenTree/contrib/container/` | 二开镜像及构建脚本 |

扫码库存变更复用 InvenTree 原生库存模型与流水；审批及来源标签提供业务约束和审计，不维护第二套库存余额。

## 前端

使用 Node.js 24（本次验证环境为 24.12.0）、Corepack 与锁定的 Yarn 依赖。Node 单元测试需要类型转换开关，只有类型擦除不足以运行 TypeScript 参数属性。

```bash
cd InvenTree/src/frontend
corepack yarn install --frozen-lockfile
node --experimental-transform-types --test tests/unit/*.test.ts
corepack yarn compile
corepack yarn build
```

`build` 包含 TypeScript 检查和 Vite 生产构建。修改翻译文案时执行 `corepack yarn extract` 并审查语言文件差异，再执行 `compile`。默认镜像构建不会自动提取全部语言目录。

## 后端

扩展运行 `stock.test_api` 等上游模块时需要相应开发依赖，例如 `pytest`；生产镜像不保证自带全部测试工具。请在独立测试容器或虚拟环境中按 `InvenTree/src/backend/requirements-dev*.txt` 安装，不要向运行中的业务容器临时安装测试依赖。

优先在独立 Docker 测试环境使用构建好的二开镜像，以保持 Python 和系统依赖与部署一致。测试数据库与媒体目录必须独立，测试期间关闭真实企业微信群 Webhook。以下命令从 `inventree-deploy/` 运行，适用于专门配置的测试环境；Django 会创建/复用 `test_wms_validation`，数据库用户需要建库权限：

```bash
docker compose run --rm \
  -e INVENTREE_DB_NAME=wms_validation \
  -e INVENTREE_MEDIA_ROOT=/tmp/wms-validation-media \
  -e INVENTREE_WECOM_BOT_WEBHOOK_URL= \
  inventree-server python manage.py test \
  stock.test_wms stock.test_approval_regressions stock.test_approval_api_contract \
  stock.test_projects stock.test_contract_warranty stock.test_wecom_notifications \
  report.test_local_print report.test_local_print_templates \
  report.test_local_print_concurrency \
  --testrunner django.test.runner.DiscoverRunner --keepdb --noinput
```

迁移回归应使用新的测试数据库，不加 `--keepdb`：

```bash
docker compose run --rm \
  -e INVENTREE_DB_NAME=wms_migration_validation \
  -e INVENTREE_MEDIA_ROOT=/tmp/wms-migration-validation-media \
  -e INVENTREE_WECOM_BOT_WEBHOOK_URL= \
  inventree-server python manage.py test \
  stock.test_migrations stock.test_contract_migrations part.test_migrations \
  --testrunner django.test.runner.DiscoverRunner --noinput
```

不要把这些命令当作业务库升级命令。迁移的实际部署流程见[部署手册](../inventree-deploy/README.md)。本地 Python 开发可按上游文档使用 Invoke 开发环境；先配置独立数据库与数据目录，再运行 `invoke dev.setup-dev`、`invoke dev.server` 和 `invoke dev.frontend-server`。

## 浏览器与硬件验收

合同流程和本地打印还提供不连接业务数据库的隔离前端用例，在前端目录运行：

```bash
node node_modules/@playwright/test/cli.js test --config playwright.contract-warranty.config.ts
node node_modules/@playwright/test/cli.js test --config playwright.local-print.config.ts
```

这些用例使用测试接口和打印驱动模拟，不会向真实打印机发送任务。

Playwright 主配置会启动前端、Django 和 worker，并执行全局测试准备。必须先准备专用测试环境及浏览器依赖，再在前端目录运行：

```bash
corepack yarn playwright install chromium
corepack yarn playwright test --project=chromium
```

测试配置及用例位于 `InvenTree/src/frontend/`；已有实例专用的预览脚本不替代测试环境初始化。端到端通过不代表打印机通过：Windows 驱动、QZ Tray 信任、标签尺寸、真实出纸、二维码识别及断网/缺纸处理仍需按[打印验收交接](WINDOWS_LOCAL_PRINT_READY_FOR_WINDOWS.md)在工位实测。

## 提交约定

每个逻辑修改单独提交，模型、迁移及其直接依赖应保持可运行；功能代码和对应测试一起提交。README 与业务说明应同步更新。不要提交 `.env`、数据库、媒体、私钥、运行日志、`node_modules/`、构建产物或本地验收备份。

`docs/*ACCEPTANCE*.md` 为历史记录，保留当时范围及结果；其中引用的本地日志、截图和运行数据不入库。新版本应依据当次测试结果判断，不沿用历史“已通过”结论。
