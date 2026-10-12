# 基于 InvenTree 二开的 WMS 技术栈

## 当前基线

- 运行版本：InvenTree `1.5.1`，底座镜像已锁定到 digest
- 二开源码：InvenTree 官方仓库 `wms-demo-1.5.1` 本地分支
- 源码目录：`../InvenTree/`
- 部署目录：当前目录

源码和镜像底座均对应 release commit `84052e1`，不再依赖浮动的 `stable` 标签。

## 后端

- 语言：Python
- Web 框架：Django 5.2
- API：Django REST Framework 3.18
- API 文档：drf-spectacular / OpenAPI
- ORM 与迁移：Django ORM + Django migrations
- 后台任务：Django Q2
- Web 进程：Gunicorn
- 身份认证：Django Allauth Headless
- API 认证：Session、Token、JWT、OAuth2，可扩展 SSO
- 安全能力：CSRF、CORS、权限组、对象/接口权限、MFA、审计与结构化日志
- 插件机制：InvenTree Plugin Framework
- 报表能力：Jinja2、WeasyPrint、PDF/标签模板
- 可观测性：django-structlog、Sentry、OpenTelemetry

InvenTree 后端采用领域模块拆分，核心包括：

- `part`：物料、产品和分类
- `stock`：库存、库位、库存流水和批次/序列号
- `order`：采购、销售、退货、调拨
- `build`：生产和装配
- `company`：供应商、客户、制造商
- `machine`：外部机器和设备
- `plugin`：插件注册、生命周期和扩展点
- `users`：用户、角色和权限
- `report`：标签、单据和报表

## 前端

- 语言：TypeScript
- UI 框架：React 19
- 构建工具：Vite 7
- UI 组件库：Mantine 9
- 表格：Mantine DataTable
- 路由：React Router 6
- 服务端状态：TanStack Query 5
- 全局状态：Zustand 5
- 表单：React Hook Form
- 国际化：Lingui
- 图表：Recharts
- 二维码与条码：`html5-qrcode`、`qrcode`，并可接收扫码枪的键盘输入
- 端到端测试：Playwright
- 代码质量：Biome、TypeScript compiler

## 数据与基础设施

- 主数据库：PostgreSQL 17
- 缓存与队列辅助：Redis 7
- 反向代理与静态文件：Caddy
- 容器编排：Docker Compose
- 数据持久化：宿主机绑定目录 `inventree-data/`
- 时区：Asia/Shanghai

InvenTree 代码还支持 MySQL、MariaDB 和 SQLite，但本项目建议沿用官方 Docker 默认的 PostgreSQL，减少二开和升级风险。

## 已实现的扫码入库模块

扫码枪会把二维码解码成一串编号，并以键盘输入加回车的方式提交。第一阶段不做摄像头识别，也不需要修改扫码枪协议。

当前二开已在 InvenTree 核心库存域中实现“成品扫码入库”模块：

1. 条码映射：已知条码使用 InvenTree Part 的 `barcode_data` 和默认库位；未知条码由扫码页收集最小商品资料后原子建档。
2. `ScanInboundRecord`：每次成功扫码保存编号、数量、时间、Part、库位、库存对象、操作人和唯一客户端事件 ID。
3. 专用 `StockItem`：每个 Part + 默认库位维护一个受约束的扫码累计库存项，禁止拆分、转移或合并。
4. 原子事务：锁定条码、Part 和专用 StockItem；新建 Part、绑定条码、库存 `+1`、`StockItemTracking` 和扫码审计记录一起提交或回滚。
5. REST API：`GET/POST /api/stock/scan-inbound/`，带分页、认证和 `stock.view` / `stock.add` 权限。
6. React 页面：自动聚焦、Enter 入队、未知条码建档弹窗、树形分类/库位选择、连续扫码、实时结果、最近流水和多条失败事件安全重试。
7. 幂等：客户端为每次真实扫码生成 UUID；网络重试复用 UUID 并返回 `replayed=true`，不会重复加库存。
8. 竞争保护：数据库级 Part 条码唯一约束和跨模型共用的条码锁防止重复建档；并发资料冲突会明确拒绝，不覆盖先提交者。

数据关系：

```text
二维码编号
  -> 已知 Part，或弹窗原子创建 Part + 绑定 barcode_data
  -> 指定 Stock Location 下的 Stock Item
  -> quantity + 1
  -> ScanInboundRecord + StockItemTracking
```

## 二开边界

- 优先使用独立 Django app / InvenTree 插件和官方 REST API，避免直接修改大量核心模型。
- 前端扫码页作为独立路由和组件接入，复用 InvenTree 的权限、通知、表格和 API 客户端。
- 数据库变更只新增迁移，不修改官方历史迁移。
- InvenTree release tag、底座镜像 digest 和前端 `yarn.lock` 已锁定；构建脚本会重新生成并验证二开前端产物。
- 扫码接口已经接入 InvenTree 用户、用户组和角色权限，不另造用户系统。
