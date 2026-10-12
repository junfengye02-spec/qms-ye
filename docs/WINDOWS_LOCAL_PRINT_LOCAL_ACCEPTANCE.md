# Windows 本地打印：本机开发验收记录

> 2026-09-20 后续部署更新：已按用户要求完成内网测试实例部署和服务检查，等待 Windows 实机准备/验收。详见 [Windows 测试交接](WINDOWS_LOCAL_PRINT_READY_FOR_WINDOWS.md)。下文“尚未部署”描述为此前开发验收时的状态。

日期：2026-09-20。范围：用户授权在 macOS 本机先开发、再模拟测试。结论只覆盖软件逻辑，不代表 Windows 实物打印已通过。

## 实现范围

- 后端：LocalPrintJob 表及新增迁移 0033；原 DataOutput/PDF 渲染；严格模板解析；按用户请求去重；对象权限/IPN/份数验证；受限 QZ 签名；数据库原子认领；未知结果与显式关联补打；会话 CSRF；本地输出不经旧 staff-wide DataOutput 接口暴露 URL。
- 前端：商品/库存现有入口按开关转入本机弹窗；固定 50 × 30 mm 标签；共享安全连接；明确选队列并在发送前检查；单次签名；禁止连点；退出登录、切换用户中断；丢失创建响应后按原 key 查询；最近任务恢复不自动发送；主动 PDF 下载。
- 部署：模板预览/备份/恢复命令；默认关闭；服务器秘密文件配置；安装指引；证书轮换、撤销和回退文档。安装包链接待验证后配置。
- 翻译：提取 41 条本次界面消息，追加合并到已有目录，提供简体中文及英文，其余语言回退英文；保留已有未提交翻译。

实现没有变更旧 DataOutput 完成钩子的自动打开语义。新弹窗直接监控本地任务，普通报告和其他标签继续使用原逻辑。

## 验证环境与命令

- 宿主：macOS，Node.js 24.12.0；QZ JS SDK 精确依赖 `2.2.5`。
- 后端：现有 `wms-preview` 容器内 Python 3.14 / Django 5.2.16，独立测试数据库，`--keepdb`。
- 默认慢测试 runner 依赖 `django_slowtests` 未安装，测试显式使用 Django `DiscoverRunner`；原有英文断言使用 `INVENTREE_LANGUAGE=en-us`。
- 浏览器：Playwright Chromium，实际 React 弹窗及真实 QZ SDK，模拟 API 和本机 WebSocket；不触及实际打印设备。

```sh
docker exec -e INVENTREE_LANGUAGE=en-us -e INVENTREE_LOG_LEVEL=ERROR wms-preview \
  python manage.py test report.tests report.test_local_print \
  report.test_local_print_concurrency report.test_local_print_templates \
  --keepdb --noinput --testrunner django.test.runner.DiscoverRunner

cd InvenTree/src/frontend
node --experimental-transform-types --test tests/unit/local-print.test.ts
node node_modules/@playwright/test/cli.js test --config playwright.local-print.config.ts
node node_modules/typescript/bin/tsc --noEmit
./node_modules/.bin/lingui compile --typescript
node node_modules/vite/bin/vite.js build --outDir /absolute/isolated/build --emptyOutDir
```

## 测试结果

| 检查 | 结果 |
| --- | --- |
| 后端原有 report 回归 | 46 项通过 |
| 本地任务、签名、模板、并发、Schema | 23 项与原报告回归合并共 69 项通过；补充的渲染异常记录测试 1 项通过 |
| 前端适配器单元测试 | 11 项通过 |
| Chromium 弹窗流程 | 4 项通过 |
| TypeScript | 通过 |
| 生产构建与翻译编译 | 通过；构建仍提示部分 bundle 超过 500 kB |
| 新接口 OpenAPI | Schema 校验通过，8 个路径均带认证 |
| 迁移一致性 | `makemigrations --check --dry-run`：无遗漏迁移 |
| Ruff / Biome / diff whitespace | 相关文件 lint、前端格式和新增 Python 文件格式通过；原 models.py 的两处既有格式差异保留 |

覆盖重点：同一去重键只生成一次；参数不同同 key 拒绝；两个数据库连接竞争只产生一条任务和一次签名；跨账号/权限撤销/未登录/CSRF 拒绝；文件、队列、copies、缩放、raw/网络调用篡改拒绝；签名/任务过期；RSA SHA512 校验；未知结果不能重新认领；补打关联原任务；模板失败时不写入及恢复。

真实 PDF 渲染验证了单件 1 份、单件 3 份、3 件各 2 份分别为 1、3、6 页，库存标签也通过，页面尺寸检查为 50 × 30 mm。组件 copies 始终是 1。

前端测试覆盖连接复用、严格版本、队列移除、不默认切换、连点、发送超时、连接丢失、签名中退出登录、弹窗卸载和未知操作。使用真实 SDK 的报文测试验证签名前后 JSON 摘要一致且含 SHA512 算法。浏览器测试覆盖不打开 PDF 的批量提交、无组件主动下载、未知结果确认补打、响应丢失后的同 key 恢复且不自动发送。

开发时发现并修复了证书 UTC 与项目测试模式无时区时间比较问题，以及 QZ 2.2.5 预签名快捷路径不附加算法字段的问题。原 report 首轮在中文部署配置下因英文断言及时间阈值失败；使用其预期的英文测试环境重跑 46 项全部通过。新增事务测试设置 `reset_sequences=True`，避免清空后重建的 ContentType ID 与项目旧固定 ID fixtures 冲突；修正后合并 69 项通过。没有修改旧测试来放宽断言。

本机日志位于 `tmp/windows-local-print-baseline/`：`backend-final.log`、`generation-failure.log`、`frontend-unit.log`、`browser-tests.log`、`frontend-build.log`、`translations.log`、`migrations.log`、`local-schema.log`。该目录还保存实施前 tracked diff、HEAD 和状态清单；既有 untracked 文件原位保留，没有自动提交或覆盖业务修改。

截图：[模拟组件提交成功](assets/windows-local-print-simulated-20260920.png)。这是模拟队列回执，不是实物出纸证据。

## 尚未执行

Windows 首装、驱动兼容、签名信任、重启自启、两台电脑真实队列隔离、缺纸/脱机、实际方向/DPI/中文、二维码扫码和仓库试点均待提供设备后执行。没有可信生产证书或已验证安装包，没有签名采购、证书费用或分发方案定版。

本次迁移只在测试数据库执行。既有运行实例未启用新功能，也未应用可选 Compose overlay。部署时按 [运维手册](../inventree-deploy/WINDOWS_LOCAL_PRINT.md) 先备份、迁移、配置证书和实测，最后开启试点。
