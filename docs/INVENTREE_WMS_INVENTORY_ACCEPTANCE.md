# WMS 基础库存功能实施与验收记录

验收日期：2026-09-07。对应 [需求说明书](INVENTREE_WMS_INVENTORY_REQUIREMENTS.md)。

本次确认范围已实现，并通过下述专项回归和浏览器验收。按用户后续意见，保留原生单位字段及校验，暂不维护中文业务单位；验收数据使用 `count`、`m`。

## 已实现内容

| 范围 | 实现与验收结果 |
| --- | --- |
| 设备资料 | 新增品牌、型号、规格；建档必填设备ID、名称、分类、单位；支持编辑、停用和导入；存在业务数据后保护设备ID及单位 |
| 项目及地点 | 项目必填，库存按设备＋项目＋实际地点匹配；父级直属与含下级查询分开，扫码不跨项目、不扣下级库存 |
| 当前库存 | 汇总／明细、资料及项目地点筛选、零库存与冻结筛选、原生占用扣除、最后库存变动时间、完整匹配结果导出 |
| 扫码与审批 | 入库备注、出库用途及备注；未知设备延迟建档；审批冻结、快照、整单原子落账、失败回滚、重试幂等及并发保护 |
| 期间记录 | 按正式落账时间、中国时区日期闭区间查询；成功记录数、设备种类数、分单位及分设备数量小计；保留人员与业务快照 |
| 设备与盘点导入 | CSV／XLSX、模板下载、预览校验、整批提交；盘点填写绝对数量；冻结保护、歧义拒绝、过期预览拒绝、提交幂等及任务审计 |
| 导出与权限 | 导出全部筛选结果，CSV 公式保护；逐行校验新增／修改权限，OAuth 只读令牌不能导入修改 |

继续复用原生 Part、StockItem、库存调整和现有审批流程，没有增加第二套库存余额或项目管理模块。盘点调整不计入成功扫码入出库统计。

## 自动化验证

- 后端专项测试及现有审批合同、审批回归、企业微信通知测试：**43 项全部通过**，其中包含 PostgreSQL 并发扣减测试。
- `makemigrations --check --dry-run`：无遗漏迁移。
- 本次后端核心改动 Ruff 检查通过；6 个相关前端文件 Biome 检查通过。
- `npm run build`：TypeScript 类型检查与 Vite 生产构建通过；中英文词条提取及编译完成。
- OpenAPI 文档已生成；新增 WMS 接口未报告 schema 错误。全项目仍有原有两个通用 Metadata View 的 schema 错误类型及枚举／别名警告，因此不将全项目 schema 检查标记为通过。
- 构建仍提示部分 bundle 超过 500 kB；本次没有开展无关的全站拆包优化。

后端覆盖包括：必填校验、原生单位、跨项目保护、父子地点、冻结与其他原生占用、审批延迟建档、自审拒绝、申请人／审批人快照、整单回滚、冻结释放、网络重试与请求内容冲突、最后一件并发出库、10 改 8 盘点、零库存、未知设备一致建档、重复行及歧义、整批失败不留部分数据、预览过期、任务归属、OAuth 权限、全量导出、中国时区边界和不同单位统计。

此结论针对本次改动及上述专项套件，并非运行了 InvenTree 全仓库的所有测试。原有扫码测试中未提供本次新增必填字段的旧用例未纳入本次通过数量。

## 浏览器实际验收

独立验收地址：[WMS 验收实例](http://localhost:18000/web/stock/scan/inbound/)。账号 `admin`，密码 `inventree`，仅用于本机隔离验收数据库。

1. 验收仓库A 的项目001：QA-E001 直属数量 5，子地点货架B 数量 3；含下级显示 8，仅本地点显示 5。
2. 实际扫码入库 QA-E001、项目001、验收仓库A，数量 +1，保存入库备注。
3. 实际扫码出库相同组合，数量 -1，保存用途“安装验收”和出库备注；正式记录可查询。
4. 上传 CSV 盘点 QA-E002：预览原数量 10、盘点数量 8、差额 -2，勾选核对后整批提交成功。
5. 导出项目001 的仓库A直属库存，结果为 QA-E001 数量 5、QA-E002 数量 8，与页面一致。
6. 检查桌面及 390px 移动端布局、中文语言加载和浏览器控制台。

证据和执行日志位于工作区 [tmp/wms-acceptance](../tmp/wms-acceptance/)：`regression.log`、`build.log`、`ruff-final.log`、`biome.log`、`migration-check.log`、`schema.log`、`count-result.png`、`posted-outbound.png`、`mobile-records.png`、`current-export.csv`。

## 数据与运行状态

新增迁移：

- `part/0155_part_brand_part_model_number_part_specification.py`
- `stock/0133_scanstockevent_approver_snapshot_and_more.py`

验收采用独立 PostgreSQL 数据库 `wms_inventory_acceptance` 和 `wms-preview` 容器，映射本机 18000 端口。回归测试通过 `wms-acceptance` 容器及 Django 测试数据库运行，使用 `--keepdb`。原有实例没有切换到新版本，原数据未清理。验收实例使用 Django 开发服务器，作为本机功能预览。

复跑后端专项测试（工作区根目录）：

```bash
docker exec wms-acceptance python src/backend/InvenTree/manage.py test stock.test_wms stock.test_approval_api_contract stock.test_approval_regressions stock.test_wecom_notifications --testrunner django.test.runner.DiscoverRunner --keepdb --noinput
```

前端构建（`InvenTree/src/frontend`）：

```bash
npm run build
```

后续正式部署应使用本次源码构建镜像，并通过正常部署流程应用上述迁移；不要把验收数据库当作正式库存数据。


## 原生库存页面增量验收（2026-09-07）

本轮已在原生页面直接增量接入，替代扫码页重复的多标签库存管理入口。用户最新决定已同步至计划：**导入为紧挨原有导出图标的独立按钮**，点击选择设备资料导入或库存盘点导入，再进入上传、预览和确认弹窗。桌面和手机均将导入、导出作为一组排列。

### 可打开的页面

- [原生库存项入口](http://localhost:18000/web/stock/location/index/stock-items)
- [验收仓库A库存](http://localhost:18000/web/stock/location/1/stock-items)
- [货架B库存](http://localhost:18000/web/stock/location/2/stock-items)

仍为本机独立 `wms-preview` 实例，账号 `admin` / `inventree`。原实例未提交、未部署、未清理；本轮没有新增迁移或对原数据库运行 migrate。

### 最终页面行为

- 保留导航、地点／库存项侧栏、原生库存表、搜索、分页、列设置、行选择及原有库存操作。主页面不挂载 WmsInventoryPanel 多标签工作区。
- 默认业务列为原生零件名称、设备ID、库存、项目号、实际地点和单位；品牌、分类、型号、规格、设备备注、库存备注、最后库存变动时间及冻结／可用数量可在原生列设置选择。已有用户的列显隐设置继续保留。
- 一个原生搜索框可搜索设备ID、名称、项目号、品牌、型号、规格和备注。精确项目、分类、品牌、型号、包含零库存、有无库存、有无冻结、含下级地点等位于原生筛选面板。
- 小计仅一行，按所有匹配结果分单位统计。原生 `/api/stock/?wms=true` 将原生查询范围同时用于列表、小计、设备汇总和中文CSV导出，分页不缩小统计范围。
- “库存工具”按需打开设备汇总、期间记录和设备资料原生表格窗口；汇总没有库存项选择或调整操作。期间记录保留方向、日期快捷范围、开始／结束日期，其余条件在筛选面板。
- 导入复用既有事务、预览失效校验、冻结保护和幂等提交能力。盘点成功后刷新原生列表及小计。
- 扫码页“当前库存”改为前往原生库存页面的按钮；期间记录按钮直接打开原生页面的记录窗口。原扫码作业、申请／审批、库存流水、扫码记录仍沿用既有流程。

### 本轮检查结果

1. 原生入口全量显示4笔验收库存，设备2种，小计 **15 count、8 m**，没有混合单位相加。
2. 原生搜索“项目002”只显示 QA-E001 的项目002库存，小计 **7 count**；按设备汇总同为7，项目数量1。
3. 在仓库A页面通过原生筛选选择项目001，并搜索 QA-E001：含下级显示父5、子3，小计 **8 count**；将“含下级地点”选为否后仅父地点，小计 **5 count**。
4. 点击原导出图标，浏览器请求保留设备搜索、精确项目及 cascade=false。下载CSV只包含 QA-E001／项目001／验收仓库A，库存5、冻结0、可用5，与页面一致。
5. 实际通过列设置切换设备ID、库存等列；实际选择库存行后，原生库存操作和打印操作恢复可用。未执行额外库存调整。
6. 从原生导入按钮上传既有盘点CSV：QA-E002 现有8、盘点8、差额0；勾选确认后任务#2整批成功。表外库存保持不变。此前10改8的证据及后台保护回归继续有效。
7. 原生设备资料列表及新建表单可打开；两种导入入口可打开。期间窗口分别查询本月成功入库和成功出库，各1行、各1 count；扫码期间记录快捷按钮可直接进入该窗口。
8. 1440px桌面和390px手机检查完成。手机 body 宽度390px，无页面横向溢出，宽表仍在原生表格内横向浏览。手机导出与导入按钮水平相邻、间隔5px；主页面保留原生侧栏及收起入口。
9. 最终原生页面浏览器控制台无错误。测试中发现并修复重复 cascade 筛选导致的面板错误、默认列 accessor 匹配问题以及窄屏工具栏拥挤问题。
10. **45项后端专项回归全部通过**（原43项加2项原生接口回归），覆盖原生分页下全量统计、父子地点、项目搜索、导出、资料搜索及零库存筛选。前端 `npm run build` 通过，Lingui中英文分别提取并编译完成；新增功能4个前端文件 Biome 检查通过。

### 证据及检查边界

证据位于 [tmp/wms-acceptance](../tmp/wms-acceptance/)：`native-regression.log`、`native-page-build.log`、`native-feature-biome.log`、`native-ruff.log`、`native-collectstatic.log`、`native-desktop.png`、`native-mobile.png`、`native-parent-inclusive.png`、`native-parent-direct.png`、`native-summary.png`、`native-count-result.png`、`native-records-final.png`、`native-export.csv`。部分操作过程截图来自最终布局微调前，最终桌面／手机主页面以 `native-desktop.png`、`native-mobile.png` 为准。

仍未运行全仓库所有测试。生产构建保留大bundle提示；公共原生表格文件有既有未使用参数等Biome提示，`stock/api.py`保留此前权限代码处的SIM103提示，本次新增代码未新增Ruff问题。原生明细保留实际StockItem行以维持原有操作；设备汇总和业务明细CSV复用后台设备／项目／实际地点聚合口径，不自动合并数据库库存项。验收实例仍是Django开发服务器，不能视作正式生产发布。
