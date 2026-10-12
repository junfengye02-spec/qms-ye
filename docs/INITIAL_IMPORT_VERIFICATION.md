# 首次导入验证记录

日期：2026-10-12。范围：将现有本地 WMS 源码、部署配置和文档首次上传至 `junfengye02-spec/qms-ye`。这是源码归档验证，不是生产上线验收。

## 本次结果

| 检查 | 结果 |
| --- | --- |
| 后端组合回归 | 165 项通过，152.963 秒 |
| 项目归档及合同历史数据迁移 | 2 项通过，14.191 秒；新建并销毁独立测试库 |
| 前端 Node 单元测试 | 27 项通过 |
| 合同/扫码 Playwright 隔离用例 | 40 项通过，覆盖两种 UUID/HTTP 环境 |
| 本地打印 Playwright 隔离用例 | 6 项通过 |
| TypeScript | `tsc --noEmit` 通过 |
| Vite 生产构建 | 通过，34.36 秒；保留既有大于 500 kB 的 chunk 提示 |
| 部署示例 | 使用临时目录和测试占位密码运行 Compose 配置校验，通过 |
| 构建脚本 | `bash -n` 语法检查通过 |
| 本次修改的 Python 文件 | Ruff F/I 检查通过；测试文件按 Ruff 格式化 |
| 上传内容 | 原有 1,916 个已检出上游文件全部保留；无子模块指针；无大于 50 MiB 的文件 |
| 凭据检查 | 未发现真实部署凭据或运行数据进入待上传清单；源码中保留的 `contrib/container/.env` 是未经修改的上游公开示例 |

## 测试范围与隔离

后端在临时 Docker 容器中挂载当前源码，使用独立 PostgreSQL 测试数据库、临时媒体目录、关闭的真实 Webhook 和缓存。为加载上游测试模块，在临时容器安装开发依赖 `pytest==9.1.1`；没有修改正在运行的应用容器依赖。测试结束后删除临时容器，迁移测试使用的独立测试库也由 Django 销毁。

165 项组合回归包含：

- `stock.test_contract_warranty`、`stock.test_projects`、`stock.test_wms`。
- `stock.test_approval_api_contract`、`stock.test_approval_regressions`、`stock.test_wecom_notifications`。
- `report.test_local_print`、`report.test_local_print_templates`、`report.test_local_print_concurrency`。
- 另外 48 项相对上游新增或修改的测试：条码/IPN 辅助函数 2 项、扫码 API 25 项、审批 API 8 项、扫码并发 4 项、商品设置 3 项、标签 5 项、条码冲突 1 项。

迁移回归运行 `stock.test_contract_migrations`，不使用 `--keepdb`。后端组合回归使用 `django.test.runner.DiscoverRunner --keepdb --noinput`。前端命令见[开发说明](DEVELOPMENT.md)。日志保留在本机临时目录，不作为仓库文件发布。

## 本次修正

检查发现企业微信异常日志可能带出 Webhook 地址，已改为仅记录异常类型，并补充密钥不进入日志的测试。早期扫码测试未携带当前必填的项目、商品分类、单位或出库用途，现已更新夹具；没有删除测试或取消库存、权限、重放和并发断言。设备编号映射测试先确认正常模型写入被拒绝，再模拟绕过模型校验的数据篡改，验证审批仍会拒绝错误映射。

## 验证边界

本次没有运行 InvenTree 上游全部测试，没有新建完整应用 Docker 镜像，也没有升级正式业务数据库。Playwright 使用隔离接口与打印驱动模拟；Windows 真机、驱动、QZ 信任、实物标签出纸和真实扫码枪仍需现场验收。以上通过结果不代表这些未执行事项已经完成。
