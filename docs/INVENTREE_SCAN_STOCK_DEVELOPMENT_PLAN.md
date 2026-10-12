# InvenTree 扫码入库、扫码出库与操作日志开发计划

> 状态：已实施并验证  
> 编写日期：2026-08-26  
> 目标项目：`/Users/jf/Desktop/wmsYe/InvenTree`  
> 本文已同步最终实施语义和验证结果。

## 1. 最终目标

在不破坏 InvenTree 原生库存模型的前提下，把当前定制的“扫码入库”改造成符合系统原有条码语义的扫码作业：

- 扫商品码入库：当前目标库位中，对兼容库存项增加数量。
- 扫商品码出库：从当前来源库位的可用库存中减少数量。
- 扫库存项码：精确定位 InvenTree `StockItem`，而不是把它误当商品码。
- 扫库位码：设置入库目标库位、出库来源库位，或在库存项详情执行原生移库。
- 未知商品码只允许在入库流程中快速建档；出库绝不自动建档。
- 所有库存数量仍以原生 `StockItem.quantity` 为唯一真相。
- 所有真实库存变化继续进入原生 `StockItemTracking` 总账。
- 每次通过认证、权限和请求格式校验并进入库存业务处理的扫码，另外记录成功、业务失败、重放、操作员和扫码上下文。
- 操作日志必须显示操作员；新日志同时保存用户关联、用户 ID、用户名和显示名快照。

## 2. 先澄清现有三个“扫码/条码”功能

当前界面中的名称容易让人误认为是同一套逻辑，实施时必须先改清楚文案。

| 入口 | 实际含义 | 是否改变数量 | 调整方案 |
| --- | --- | --- | --- |
| 查看条码 | 查看当前 Part 或 StockItem 的系统内部二维码 | 否 | 保留，文案改成“查看库存项二维码”或“查看物料二维码” |
| 关联二维码 | 把一个历史或第三方二维码关联到当前 Part 或 StockItem，取决于当前详情对象 | 否 | 仅作兼容入口；扫码作业的商品主码统一使用 IPN，不能借此增减库存 |
| 库存项详情里的“扫码入库” | 扫描库位码，把当前整条 StockItem 移动到该库位 | 否 | 重命名为“扫描库位并移库” |
| 当前定制“扫码入库”页面 | 扫商品码，对库存增加 1；未知码可建 Part | 是 | 重构为本计划的扫码作业 |

这三者不应共用同一个含糊名称，也不应互相覆盖。

## 3. 条码与业务对象的固定关系

| 扫到的码 | 识别对象 | 用途 |
| --- | --- | --- |
| 公司商品 IPN，例如 `6901234567890` | `Part` | 商品唯一业务编号，同时用于扫码、订单、BOM 和搜索 |
| InvenTree 物料码，例如 `INV-PA#` | `Part` | 表示“这是什么物料” |
| InvenTree 库存项码，例如 `INV-SI#` | `StockItem` | 表示“具体哪一批/哪一条库存” |
| InvenTree 库位码，例如 `INV-SL#` | `StockLocation` | 表示“从哪里出、入到哪里、移到哪里” |

未知商品码在快速建档后直接成为新 `Part.IPN`，不能再生成第二套 Part 商品条码，也不能绑定成某一条 `StockItem` 的内部二维码。非空 IPN 在公司范围内忽略大小写唯一，保存前去除首尾空格。

第一次入库时，如果目标库位中没有兼容库存项，系统会创建一个普通 `StockItem`，因此此时：

- 新 `Part` 的 IPN 就是刚扫描的商品码，并仍可查看自己的 `INV-PA#` 系统物料二维码；
- 新 `StockItem` 也有自己的库存项二维码；
- 商品 IPN 只代表 `Part`，`INV-SI#` 才精确代表一条库存；
- IPN、`INV-PA#` 和 `INV-SI#` 可以关联到同一笔库存业务，但对象语义不能混淆。

## 4. 核心设计决策

### 4.1 必须遵守

1. `StockItem.quantity` 是唯一库存余额，扫码事件表不保存可被累加的第二份库存。
2. 扫码数量变化必须调用 InvenTree 原生库存方法，不能直接 `PATCH quantity`。
3. `StockItemTracking` 是库存事实总账；扫码事件表是扫码过程审计，两者职责不同。
4. 入库、出库共用一个后端领域服务，不能各自实现一套幂等、锁和候选选择逻辑。
5. 服务端从 `request.user` 获取操作员；请求体不得传入或覆盖操作员。
6. 所有新扫码写操作要求已登录用户和对应库存权限。
7. 普通可互换商品按明确库存上下文聚合；V1 不对序列号 StockItem 做扫码数量增减。
8. 新功能不再创建或依赖 `is_scan_inbound_item=True` 的特殊库存项。
9. 扫描商品码与 `Part.IPN` 是同一个字段；快速建档不再提供第二个可编辑 IPN，也不向 Part 自定义条码字段重复写入同一个值。

### 4.2 初版暂不增加 `ScanStockBucket` 映射表

初版用“锁定 Part + 在锁内重新查询明确匹配的 StockItem”选择聚合目标，减少新的缓存/映射结构。若候选不能唯一确定，必须让操作员选择或创建新项，不能按主键猜测批次。若上线后证明候选查询存在性能问题，再增加只负责选择的映射表；该表即使增加，也不能保存数量。

### 4.3 出库业务边界

初版“扫码出库”定义为无单据的直接库存领用/扣减，适合仓库日常领料或简易出库：

- 非序列号普通商品支持商品码或库存项码出库；
- 正式销售订单出货继续使用 InvenTree 原生 Sales Order 分配与发货流程；
- 已分配给订单的数量不能被直接扫码出库占用；
- 原生 `add_stock()` 和 `take_stock()` 都拒绝序列号 StockItem，因此 V1 无论扫商品码还是精确库存项码，都返回 `SERIALIZED_STOCK_REQUIRES_ORDER_FLOW`，提示改走采购接收、销售发货或原生序列号状态流程；不能用盘点为 0、物理删除或绕过模型模拟出库。

上线前仍需业务负责人确认：是否接受“初版直接出库，不自动生成 Sales Order”。本计划推荐接受，以免把简单扫码和销售履约强行混在一起。

## 5. 目标架构与数据流

```text
商品码 / INV-PA ──> Part ───────────────┐
INV-SI ──────────> 精确 StockItem ──────┤
INV-SL ──────────> 来源/目标库位 ──────┤
                                           v
                                ScanStockService
                               /                \
                      入库 add_stock      出库 take_stock
                               \                /
                                v              v
                         StockItem.quantity（唯一库存）
                                  |
                    +-------------+-------------+
                    v                           v
          StockItemTracking              ScanStockEvent
          库存事实总账                    扫码过程与操作员
```

成功扫码必须在同一个数据库事务中完成：库存变化、原生 Tracking、扫码成功事件。任一环节失败，三者一起回滚。

业务校验失败使用“外层事件 claim 事务 + 内层库存 savepoint”：只回滚内层库存操作，再把同一条 PENDING 事件更新为 FAILED 后提交外层事务。失败事件的实际变更数量为 0，且不能生成成功 Tracking。技术性 `5xx` 才回滚整个外层事务和 claim。

扫码单笔事务不能进入原生 `batch_tracking_entries()` 上下文。该上下文会通过 `transaction.on_commit()` 在主事务提交后再批量创建 Tracking，无法满足“三者一起成功或一起回滚”。扫码路径应让 `add_stock()` / `take_stock()` 直接在当前 `atomic()` 内保存 Tracking；插件事件和非关键后台任务可以在提交后发送。

## 6. 页面与交互设计

### 6.1 导航和路由

- 一级导航由“扫码入库”改成“扫码作业”。
- 使用一个共享页面、两个明确路由：
  - `/stock/scan/inbound`：扫码入库；
  - `/stock/scan/outbound`：扫码出库。
- 原 `/stock/scan-inbound/` 保留一个发布周期并重定向到新入库路由。
- 页面顶部用明显的“入库 / 出库”模式切换；颜色、图标和成功提示音不同，避免在错误模式连续扫描。
- 切换模式时清空未提交候选和库位上下文，并要求再次确认。

### 6.2 页面分区

共享页面拆为三个页签：

1. **扫码作业**：库位上下文、扫描输入、最近结果和待处理冲突。
2. **当前库存**：复用原生 `StockItemTable`，不复制库存 CRUD。
3. **操作记录**：再分“库存流水”和“扫码记录”两个子页签。

`ScanInbound.tsx` 当前是大型单文件，实施时拆分为：

- `ScanStockPage.tsx`：模式、路由和页面容器；
- `ScanWorkPanel.tsx`：扫码状态机；
- `ScanResultList.tsx`：当前会话结果；
- `ScanStockEventTable.tsx`：正式扫码事件列表；
- 原生 `StockItemTable`、`StockTrackingTable` 继续复用。

### 6.3 入库状态机

```text
选择/扫描目标库位
        |
        v
扫描商品码或库存项码
        |
        +-- 已知 Part + 唯一明确匹配项 --> 数量 +1
        |
        +-- 已知 Part + 无匹配项 --> 新建普通 StockItem，数量 1
        |
        +-- 多批次/属性候选 ------> 选择库存项或明确新建
        |
        +-- 未知商品码 ----------> 以扫码值作为 IPN 快速建 Part + 首次入库
        |
        +-- 序列号/业务校验失败 --> 失败，不改库存，写失败扫码事件
```

具体规则：

- 目标库位必须显式扫描或选择；Part 默认库位可以预填，但页面必须让操作员看见并确认，不能后台悄悄使用。
- 商品码每次默认增加 1，后续可提供“本轮固定数量”设置，但请求数量必须大于 0。
- 入库请求 DTO 除 `client_event_id`、`barcode`、`destination_location`、`quantity` 外，还支持显式 `stock_item_id` 或 `create_stock_item=true`，以及需要时的 `batch`、`status`、`packaging`、`supplier_part`、`expiry_date`；这些字段都进入 fingerprint 和锁内二次校验。
- 未提供批次等上下文时，只允许自动聚合**唯一一条**“中性库存项”：同 Part/Location、空 batch、默认可用 status、空 packaging、无 supplier/expiry、非序列号。出现多条中性项或任何非中性候选时返回 `STOCK_SELECTION_REQUIRED`，由操作员选择现有项或明确新建，绝不按最小主键猜批次。
- `STOCK_SELECTION_REQUIRED` 是不认领事件 UUID 的只读预检结果；操作员选择 `stock_item_id` 或“新建”后复用原 UUID 提交最终请求，服务此时才 claim。取消选择可另写 `CANCELLED_SELECTION`、实际变化量 0 的事件。
- 操作员显式选择 `stock_item_id` 时，服务仍要在行锁内核对 Part、库位、批次、状态和可调整性；前端选择不能替代服务端验证。
- 未知商品码以扫码值作为 IPN 的快速建档和首次库存增加应在同一事务内完成。
- 未知码第一次解析返回 `SETUP_REQUIRED` 时只是建档预检，不 claim UUID，也不持久化最终 fingerprint；操作员提交快速建档表单后复用原 UUID，服务才认领事件并执行唯一一次事务。最终 fingerprint 必须包含所有会产生副作用的规范化建档字段（名称、由扫码值确定的 IPN、分类、默认库位、描述等）；claim 后同 UUID 改建档资料必须返回 409 或要求新 UUID，不能静默重放第一份资料。
- 操作员取消快速建档时可写 `CANCELLED_SETUP` 扫码事件，实际变化量为 0。
- 快速建档要求 Part 新建权限和库存新增权限，不要求修改已有 Part；任一不足不得留下半成品 Part。
- 未知码快速建档的首个库存项默认使用中性库存上下文；若该物料必须按批次、包装、供应商或有效期管理，表单必须先收集这些字段，不能把数量写进不确定的旧批次。
- V1 对序列号 Part/StockItem 返回 `SERIALIZED_STOCK_REQUIRES_ORDER_FLOW`，不调用 `add_stock()`。
- `SCAN-INBOUND` 批号只保留为旧数据，不再自动写给新库存。

### 6.4 出库状态机

```text
选择/扫描来源库位
        |
        v
扫描商品码或精确库存项码
        |
        +-- 精确非序列号 StockItem 可用 -> 数量 -1
        |
        +-- 商品码只有一个可用候选 ----> 数量 -1
        |
        +-- 同库位多个批次/候选 --------> 要求操作员选择
        |
        +-- 序列号/未知/不足/已分配 ----> 失败，不改库存，写失败扫码事件
```

具体规则：

- 初版要求来源库位显式扫描或选择，不跨库位静默扣减。
- 扫 `INV-SI#` 时精确定位 StockItem；来源库位上下文与库存项实际库位冲突时拒绝并提示。
- 扫 Part 商品码时，只在来源库位内查找可用、未过期、未冻结且未安装/未交付的库存项。
- 加锁后重新检查 `unallocated_quantity >= requested_quantity`，再调用原生扣减。
- 调用 `take_stock()` 的对象必须是刚由 `select_for_update()` 取出并刷新过的同一个 `locked_item`；不能在旧实例上先按陈旧 `self.quantity` 截断数量，再期待方法内部加锁纠正。
- 不能只依赖 `take_stock()` 判断库存不足，因为该方法可能把请求量截断到当前数量；服务必须在行锁内先做严格校验。
- 同库位出现多个批次时，初版要求人工选择，先保证可解释性；FEFO/FIFO 自动规则作为后续可配置增强。
- 多候选响应与入库一样是非终态预检，不把原 UUID 写成最终 FAILED；操作员选择候选后复用该 UUID，`stock_item_id` 进入 fingerprint，再在行锁内复验。
- 未知商品码永不创建 Part。
- V1 对序列号 Part/StockItem 返回 `SERIALIZED_STOCK_REQUIRES_ORDER_FLOW`，不调用 `take_stock()`。
- 出库为 0 后是否保留空库存项遵循 InvenTree 原生 `delete_on_deplete` 设置；扫码事件和 Tracking 必须继续可查。

## 7. 库存数量增删改查入口

扫码不是唯一库存操作入口，计划同时把原生入口和中文文案整理清楚。

| 操作 | 正确入口 | 说明 |
| --- | --- | --- |
| 查库存 | 库存列表、Part 库存页、StockItem 详情 | 可按物料、库位、批次、状态筛选 |
| 新建库存项 | 库存列表绿色 `+` | 创建一条 StockItem，不等于给现有项加数量 |
| 增加数量 | 选中库存项 → “添加库存” | 调用原生 `/api/stock/add/` |
| 减少数量 | 选中库存项 → “移除库存” | 调用原生 `/api/stock/remove/` |
| 盘点数量 | 选中库存项 → “盘点库存” | 调用原生 `/api/stock/count/`，设置实际盘点数 |
| 移库 | 选中库存项 → “转移库存”或详情“扫描库位并移库” | 改库位，不改变总数量 |
| 编辑属性 | StockItem 详情的编辑操作 | 批次、状态、包装等；不是直接改数量 |
| 业务清零 | “移除库存”或“盘点库存 = 0” | 保留审计链，不把物理删除当日常减库存 |

前端需修正已有误导翻译：

- `Add Stock Item` → “新建库存项”；
- `Add Stock` → “添加库存”；
- `Remove Stock` → “移除库存”；
- `Count Stock` → “盘点库存”；
- 库存项详情原“扫码入库” → “扫描库位并移库”。

## 8. 数据模型计划

### 8.1 新增 `ScanStockEvent`

不要直接一次性重命名旧表。先加新表和新 writer；停写窗口按旧表主键 high-water 分批幂等回填并核对，随后读取切到新表。旧表只作为一个发布周期的回滚依据，不做长期双写，也不让页面同时混读两张结构相同的流水表。

建议字段：

| 字段 | 含义 |
| --- | --- |
| `client_event_id` | 客户端为一次物理扫码生成的 UUID，数据库唯一 |
| `request_fingerprint` | 方向、条码、操作员、库位、批次等规范化输入的 SHA-256 |
| `fingerprint_version` | fingerprint 规范版本；新事件从 `v1` 开始，旧迁移事件标为 `legacy-v0` |
| `operation` | `INBOUND` / `OUTBOUND`，数据库 CheckConstraint |
| `outcome` | `PENDING` / `SUCCESS` / `FAILED` |
| `error_code` | 稳定的机器错误码，例如 `UNKNOWN_BARCODE`、`AMBIGUOUS_STOCK` |
| `error_message` | 脱敏后的操作提示，不保存堆栈 |
| `barcode` | 原始码，最大 500；列表默认掩码，按权限查看完整值 |
| `barcode_hash` | 条码哈希，便于索引检索 |
| `barcode_kind` | `PART` / `STOCK_ITEM` / `LOCATION` / `EXTERNAL` |
| `requested_quantity` | 请求数量，必须大于 0 |
| `applied_quantity` | 实际变化的绝对数量；方向由 operation 表示，失败/PENDING 为 0，成功等于 requested |
| `part` | 可空 `SET_NULL` FK |
| `part_pk/name/ipn_snapshot` | 删除或改名后仍可审计 |
| `stock_item` | 可空 `SET_NULL` FK，不能继续使用 `PROTECT` |
| `stock_item_pk/batch_snapshot` | 库存项和批次快照 |
| `source_location` | 出库来源，可空 `SET_NULL` FK |
| `destination_location` | 入库目标，可空 `SET_NULL` FK |
| `source/destination_path_snapshot` | 完整库位路径快照 |
| `stock_item_quantity_before/after` | 当前库存项变化前后快照 |
| `part_total_before/after` | Part 总库存变化前后快照 |
| `operator` | 可空 `SET_NULL` FK；新的人工作业不可为空 |
| `operator_user_id_snapshot` | 操作时用户 ID 快照 |
| `operator_username_snapshot` | 操作时用户名快照 |
| `operator_display_name_snapshot` | 操作时统一 display resolver 的结果，最终回退 username |
| `actor_kind` | `USER` / `SYSTEM` / `API`，不把系统任务伪装成人 |
| `device_id` / `session_id` | 可选终端和会话标识 |
| `replay_count` / `last_replayed_at` | 网络重放次数及最近重放时间；重放不新增库存动作 |
| `scanned_at` / `completed_at` | 扫码和处理完成时间；`scanned_at` 用可显式覆盖的 default，不用会覆盖迁移旧时间的 `auto_now_add` |
| `legacy_inbound_record_pk` | 旧流水迁移核对用，唯一且可空 |

建议索引：

- `(-scanned_at, -id)`；
- `(operation, -scanned_at)`；
- `(outcome, -scanned_at)`；
- `(part, -scanned_at)`；
- `(operator, -scanned_at)`；
- `(barcode_hash, -scanned_at)`。

`stock_item_quantity_before/after` 是本次动作的权威数量快照。`part_total_before/after` 只用于页面上下文，不作为账务不变量；否则为了一个聚合值需要锁住该 Part 的全部库存项，会放大并发锁范围。库存核对仍按实时 `StockItem.quantity` 汇总。

数据库条件约束固定为：`requested_quantity > 0`；PENDING/FAILED 的 `applied_quantity = 0`；SUCCESS 的 `applied_quantity = requested_quantity` 且 `completed_at` 非空。`actor_kind=USER` 的新记录在服务层强制有 operator，但不能做永久 NOT NULL/CheckConstraint，因为用户删除后 FK 必须允许 `SET_NULL`，身份由快照保留。

事件写入边界：只有已经认证、具备扫码权限，并且请求 envelope、UUID、barcode 和 quantity 格式合法的请求才创建 `ScanStockEvent`。未知出库商品、库存不足、选择后状态失效、序列号不支持等属于可审计业务失败；`SETUP_REQUIRED` / `STOCK_SELECTION_REQUIRED` 属于尚未执行的非终态预检，不 claim UUID，提交建档或选择结果后才创建正式事件。未登录、无扫码权限、空/超长条码、非正数量等在进入领域服务前返回 4xx，进入 `BarcodeScanResult`（若启用）或安全/运维日志，不写正式库存事件。

### 8.2 操作员记录规则

- API 只取 `request.user`，不接受客户端提交 `operator`。
- `ScanStockEvent` 和 `StockItemTracking` 必须调用同一个 `operator_snapshot(user)` resolver：ID 取 `user.pk`，username 取 `get_username()`，display name 取操作当时 InvenTree 的 canonical display resolver / `str(user)`，为空再回退 full name、最后回退 username；不能两张表各自拼姓名。
- 用户 FK 只用于当前账号跳转；读取时始终以三个快照为历史权威，防止用户改名、停用、删除或显示名设置变化后历史漂移。
- 用户账号优先停用而不是硬删除。
- 旧记录没有用户时显示“历史记录（未知操作员）”，数据库保持 `operator=NULL`，不能伪造为某个管理员。
- 旧记录的姓名只能按迁移时用户资料回填，计划和上线说明必须注明“并非操作发生时的原始姓名”。

### 8.3 原生 `StockItemTracking` 的操作员快照与扫码关联

原生 Tracking 已有 `user` FK，但用户删除后会变成空。为使 Tracking 在其原生生命周期内稳定显示操作员，并能精确链接扫码事件：

- 为 `StockItemTracking` 增加可空的 `operator_user_id_snapshot`、`operator_username_snapshot`、`operator_display_name_snapshot`；
- 为 `StockItemTracking` 增加可空 `scan_event=ForeignKey(ScanStockEvent, SET_NULL, related_name='tracking_entries')`；采用一对多是为了兼容未来一个事件产生多条原生事实，V1 普通扫码仍要求恰好一条相关 Tracking；
- 提供一个统一的 `operator_snapshot(user)` / Tracking 构造 helper，并让 `add_tracking_entry()` 使用它；
- 代码中仍有直接构造并 `bulk_create(StockItemTracking)` 的采购、订单、构建等路径，实施时必须逐处改用同一 helper，因为 `bulk_create` 不会调用模型 `save()`；不能只改 `add_tracking_entry()` 就宣称覆盖全部操作；
- 现有 `user` FK 保留用于过滤和当前用户跳转；
- schema migration 只增加无默认值的可空字段；历史数据用可断点续跑的批处理分批回填，避免在一次数据库迁移中长时间锁住大表；
- 无法确定的历史操作员保持空并显示“未知操作员”；
- 扫码事务先创建 `ScanStockEvent(PENDING)` 取得主键，再让受支持的 `add_stock()` / `take_stock()` 参数把该对象传给本次 Tracking；不能把未知 kwarg 交给现有方法后假设它会自动保存；
- Tracking serializer、filter、导出和前端详情都增加 `scan_event`，禁止用“时间最接近的一条”猜关联。

`StockItemTracking.part` 当前采用 `CASCADE`。本次不改变 InvenTree 对原生 Tracking 的 Part 删除生命周期，因此这里的承诺是“Tracking 存续期间，用户改名或删除不丢操作员”；若 Part 被物理删除，永久扫码审计由采用 `SET_NULL + Part 快照` 的 `ScanStockEvent` 保留。管理员界面应明确提示物理删除的审计影响。

### 8.4 幂等语义

- 一次物理扫码只生成一个 `client_event_id`。
- 网络重试复用原 UUID；同用户、同方向、同 fingerprint 的成功记录直接重放响应，不再改库存。
- 同 UUID 换用户、方向、条码、库位、批次或数量返回 `409 IDEMPOTENCY_CONFLICT`。
- 操作员修改输入后重新提交属于新业务尝试，前端必须生成新 UUID。
- 唯一例外是未 claim 的 `SETUP_REQUIRED` / `STOCK_SELECTION_REQUIRED` 预检：补充建档资料或候选 `stock_item_id` 后复用原 UUID，最终 fingerprint 包含选择结果。
- 业务失败可保存为最终失败事件；相同 UUID 重放同一失败结果。
- 技术性 `5xx` 不写成最终业务失败，使相同 UUID 可以安全重试。
- 每次重放只更新 `replay_count` 和 `last_replayed_at`；不新增事件、不新增 Tracking、不重复播放“库存已变化”的反馈。
- 迁移事件使用 `fingerprint_version=legacy-v0`，兼容端点先按 UUID 查找旧成功事件，并只按旧契约的 barcode + operator 校验后重放；不能因为迁移后默认库位、批次或新 DTO 字段变化，把旧网络重试误判为 409。

### 8.5 不把 `BarcodeScanResult` 当业务日志

InvenTree 原有 `BarcodeScanResult` 适合条码解析调试，但它默认可关闭、默认只保留有限记录，且当前定制扫码接口不会自动进入这张表。因此：

- 它继续作为低层调试日志；
- 正式扫码入/出库审计使用 `ScanStockEvent`；
- 正式库存数量变化使用 `StockItemTracking`；
- 不用三套表重复记录同一种库存事实。

### 8.6 日志留存契约

- `ScanStockEvent` 自身保存足够的 Part、StockItem、库位、数量和操作员快照；V1 不自动清理，是扫码业务的长期审计记录。
- 原生 `StockItemTracking` 可能受 `STOCK_TRACKING_DELETE_OLD_ENTRIES` 保留策略清理，也会随 Part 物理删除按原生规则删除；因此 `scan_event` 关联允许未来缺失，页面显示“原生库存流水已按保留策略清理”，扫码事件本身仍可读。
- 上线清单必须记录当前 Tracking 保留设置。若业务要求原生库存流水永久保存，应关闭自动清理，并先评估表容量、索引、备份和归档成本；不能只在页面上承诺“永久”。
- 日志导出与删除权限单独控制；普通仓库用户不得清理正式扫码事件。

## 9. 后端 API 与服务

### 9.1 API

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/api/stock/scan/inbound/` | 商品扫码入库 |
| `POST` | `/api/stock/scan/outbound/` | 商品扫码出库 |
| `GET` | `/api/stock/scan/events/` | 查询/过滤/导出扫码事件 |
| `GET` | `/api/stock/track/` | 继续查询/导出原生库存总账 |

旧 `/api/stock/scan-inbound/` 在兼容期调用新服务并保持旧响应字段，前端迁移完成后再下线。

扫码事件列表至少支持：方向、结果、错误原因、起止时间、操作员、Part、StockItem、来源库位、目标库位、条码哈希和事件 UUID；必须分页并复用 InvenTree 的导出能力。

`ScanStockEvent` 只开放领域动作 POST 和只读 list/detail/export，不注册通用 create/update/import API；operator、快照、outcome、数量前后值和关联对象全部由服务端写入。

### 9.2 `ScanStockService`

把当前堆在 DRF serializer `create()` 中的业务拆到单一领域服务，职责为：

1. 校验权限、模式和请求；
2. 解析 InvenTree 原生码、公司商品 IPN 和兼容的历史自定义条码；
3. 认领事件 UUID、校验 fingerprint、处理重放；
4. 入库未知 IPN 的快速建档；
5. 选择或创建兼容 StockItem；
6. 出库候选过滤和歧义返回；
7. 统一数据库锁顺序；
8. 调用原生 add/remove 方法；
9. 创建 Tracking 关联和事件快照；
10. 生成稳定、可测试的 API 响应。

不要复制 InvenTree 的 Add、Remove、Count、Transfer serializer 作为“扫码专用 CRUD”。扫码服务只做识别、选择、并发和审计，然后复用原生库存领域方法。

Tracking 关联的实现契约：

- `ScanStockEvent(PENDING)` 先在事务中取得主键；
- `add_stock()` / `take_stock()` 显式支持内部参数 `scan_event`，并把它传入 `add_tracking_entry()` 的 `StockItemTracking.scan_event` 字段；
- API serializer、`StockTrackingFilter`、全局导出和前端详情都能按 `scan_event` 精确查询和跳转；
- 无匹配项的新建路径固定为 `StockItem(quantity=0).save(add_note=False)`，随后统一调用 `add_stock(requested_quantity, scan_event=event)`，只生成一条关联的 `STOCK_ADD`；不能先生成 `CREATED` 再额外生成 `STOCK_ADD` 而仍声称“恰好一条”。

### 9.3 事务和锁顺序

所有写路径固定采用以下顺序，降低死锁概率并保证同 UUID 并发结果确定：

1. 做无副作用的认证、基础格式校验和未知入库预检；
2. 进入外层 `transaction.atomic()`；在额外的 claim savepoint 中尝试插入 `ScanStockEvent(PENDING)`，把唯一键 `IntegrityError` 限制在该 savepoint 内，避免把整个外层事务标记为 rollback-only；
3. 唯一冲突时退出 claim savepoint，锁定并读取赢家事件；相同 UUID 的其他请求等待赢家提交，再按 fingerprint 重放或返回 409；
4. 进入内层 savepoint，依次取得未知 IPN 注册锁、Part 行锁和按主键升序的候选 StockItem 行锁；
5. 加锁后重新验证 Part、库位、批次、状态、数量和 allocations；
6. 调用原生数量方法并在内层直接保存 Tracking；
7. 成功时更新同一事件为 SUCCESS，并随外层事务一次提交；
8. 可预期业务异常只回滚内层 savepoint，外层把同一事件更新为 FAILED 后提交；
9. 非预期技术异常回滚整个外层事务，包括 PENDING claim，使原 UUID 可安全重试。

同一个 UUID 并发多次只能有一次真正改库存；两个不同出库事件争最后一件只能有一个成功。

对扫码路径的事务约束：不使用 `batch_tracking_entries()`；Tracking 创建异常必须让数量变化和成功事件一起回滚。`batch_events()` / 后台任务只能承载提交后的通知，不能承担业务审计持久化。同 UUID 并发业务失败、以及一条请求失败/另一条竞争请求等待的情况，都必须有专门事务测试。

`replay_count` 使用锁定事件行或 `F('replay_count') + 1` 原子更新，避免并发重放丢计数。claim 逻辑可以沿用当前定制 serializer 已采用的“内层 savepoint 捕获唯一冲突”模式，但必须提取到共享服务。

## 10. 操作日志页面

在扫码作业页和库存模块中提供同一个“操作记录”入口，但不把两种不同事实强行拼成一张可编辑表。

### 10.1 库存流水

数据源：`StockItemTracking`。

覆盖：新建、添加、移除、盘点、移库、批次/状态变化、采购接收、订单发货等实际库存变化。采用“扩展复用”现有 `StockTrackingTable`：新增 `scope='global'|'part'|'item'`，全局模式请求 `part_detail=true`、`item_detail=true`、`user_detail=true` 并显示 Part/IPN/StockItem，不能原样复用后得到缺少对象身份的空列。

主要列：

- 时间；
- 操作类型；
- 操作员快照；
- Part / IPN；
- StockItem / 批次；
- 本次数量变化；
- 操作后数量（仅能从该 tracking type 可靠得出时显示）；
- 来源/目标库位；
- 业务单据；
- 备注；
- 关联扫码事件。

`StockItemTracking.deltas.quantity` 在不同类型中含义不同：Add/Remove 时通常是操作后余额，Move 和销售发货时通常是本次数量。后端 serializer 和前端必须按 `tracking_type` 解释，不能统一把它标成“操作后数量”；无法可靠推导的旧记录显示“本次数量”或留空。新 Move Tracking 还要记录 `old_location`，delta model map 和表格显示“来源 → 目标”；旧 Move 记录没有来源时明确显示未知。

Tracking API/serializer 需要补充三个操作员快照、`scan_event`、全局对象详情和 `old_location` 解析；JSON、CSV/XLSX 导出和 UI 必须应用同一字段与权限规则。用户 FK 已为空时，页面回退显示操作员快照。三个快照和 `scan_event` 必须同时列入 serializer `read_only_fields` 与 importer 排除字段（或取消该模型的 importer 注册），只能由领域服务/helper 内部写入，防止通过数据导入伪造操作员或关联。

### 10.2 扫码记录

数据源：`ScanStockEvent`。

同时显示成功和业务失败，用于回答“谁、何时、在哪个模式、扫了什么、为什么成功或失败”。

主要列：

- 扫码时间和完成时间；
- 入库/出库；
- 成功/失败/重放；
- 操作员显示名、用户名、用户 ID；
- 商品、IPN、条码（默认掩码）；
- 请求量和实际量；
- 变化前 → 变化后；
- 来源/目标库位；
- 批次和 StockItem；
- 错误原因；
- 终端和事件 UUID。

普通查看页面、搜索、打开条码弹窗不属于库存操作，不写业务操作日志。认证攻击、权限攻击和服务异常进入安全/运维日志，不在普通库存日志里暴露完整原始条码。

## 11. 权限矩阵

| 能力 | 权限要求 |
| --- | --- |
| 查看扫码页和库存 | `stock.view_stockitem` + 对应 `stock.view` 角色范围 |
| 给已有 StockItem 入库 | `stock.change_stockitem` + 对应 `stock.change` 角色范围 |
| 入库需要新建 StockItem | 上述权限 + `stock.add_stockitem` |
| 未知码快速建档并入库 | 上述实际库存权限 + `part.add_part`（无需 `part.change_part`） |
| 扫码出库 | `stock.change_stockitem` + 对应 `stock.change` 角色范围 |
| 查看扫码事件 | 与库存流水查看权限相同；可按策略限制为本人/授权库位 |
| 查看完整原始条码和全量导出 | 审计或管理员权限 |
| 查看库存 Tracking | 复用原生权限 |

现有前端库存数量操作按 `stock.change` 判断，但后端 `StockAdjustView` 使用 POST 默认模型权限，二者可能不一致。实施时要给扫码端点和原生 Add/Remove/Count/Transfer 端点显式声明上述权限契约，不能继续依赖 HTTP POST 自动映射成 add 权限；V1 不新增含义模糊的“出库角色”。必须直接调用 API 测试 view-only、add-only、change-only、add+change 和管理员五类账号，确保没有只持 add 权限却能扣库存的越权路径。

## 12. 旧定制数据迁移

采用“先加新结构，再切写入，稳定后删除旧结构”的两阶段迁移。

### 12.1 发布 A：兼容结构

1. 新增 `ScanStockEvent`、约束和索引。
2. 新增 Tracking 操作员快照字段。
3. 新增只读扫码事件 API，旧扫码写路径暂不变化。
4. 在生产数据副本执行迁移和性能检查。

### 12.2 受控切换窗口

1. 停止 Web 和 worker 写入，防止新旧逻辑同时创建特殊库存项。
2. 备份数据库并记录迁移前统计。
3. 记录 `MAX(ScanInboundRecord.pk)` high-water；用可断点续跑的管理命令按 pk 分批回填，每批独立事务，并按唯一 `legacy_inbound_record_pk` 执行 update-or-create。
4. 保留原 UUID、条码、数量、时间、Part、库位、StockItem 和用户。
5. 根据旧快照计算 before/after；缺失信息保持空并标明 `legacy`，不得猜测。
6. 已完成新事件快照回填后，把旧流水指向 Part、Location、StockItem 的 `PROTECT` 改为允许空的 `SET_NULL`；旧 serializer 仍写入完整 FK，但历史记录不再阻止库存项耗尽删除。
7. 新服务停止创建新的 `is_scan_inbound_item=True` 项，但发布 A 观察期暂不清除旧 flag、不删除旧唯一约束和旧业务保护分支。
8. 旧特殊 StockItem 原地保留主键、数量、库位、批次、状态和 flag；新服务将它们标为 legacy 候选，不静默合并或改属性。
9. 启用新扫码服务，启动 Web 和 worker；旧扫码写端点关闭，只保留兼容重定向/只读查询。
10. 核对 Part 总库存、StockItem 数量和事件数量。

旧流水回填规则固定为：`operation=INBOUND`、`destination_location=old.location`、`stock_item_quantity_after=old.stock_item_quantity`、`stock_item_quantity_before=after-old.quantity`、`part_total_after=old.new_total_stock`、`part_total_before=after-old.quantity`、`legacy_inbound_record_pk=old.pk`。旧数据没有真实历史批次或姓名快照时只能取迁移时值并标记来源，不能描述成“操作当时值”。

每批记录计数、旧/新主键集合和关键字段 checksum，失败可从上个 pk 继续。旧 `scanned_at` 必须原值写入；legacy `completed_at` 统一取 `scanned_at` 并标记 legacy 来源，以满足成功事件约束。Tracking 操作员快照也按 high-water 分批回填，切换前再做一次收尾增量。

迁移禁止：

- 新建替代库存项；
- 自动移动或合并旧库存；
- 清空 `SCAN-INBOUND` 批次；
- 根据历史扫码流水重新计算并覆盖当前余额；
- 按时间接近程度猜测旧 Tracking 关联。

### 12.3 发布 B：观察后清理

稳定运行至少一个发布周期后：

- 再次停写和备份；
- 将旧特殊 StockItem 原地转为普通 StockItem，保持主键、数量、库位、批次和状态不变；
- 清除旧 flag，移除特殊唯一约束；
- 删除旧 `ScanInboundRecord` 写入和读取代码；
- 删除旧表；
- 删除 `is_scan_inbound_item` 字段，以及转移/拆分/合并等遗留保护分支；
- 删除兼容路由；
- 保留迁移报告和数量核对结果。

## 13. 实施阶段与依赖

### 阶段 0：契约和基线

- 确认初版直接出库边界。
- 固定库位必选、多候选人工选择、已分配库存不可出库等规则。
- 记录迁移前库存总量、旧事件数和特殊库存项数。
- 给当前定制入库补充基线测试，防止重构时丢失已有幂等能力。

### 阶段 1：事件模型与操作员

- 新建 `ScanStockEvent`。
- 增加 Tracking 操作员快照、`scan_event` FK 和 Move 的 `old_location` delta。
- 扩展 Tracking serializer/filter/export，输出操作员快照、扫码关联和全局对象详情。
- 实现扫码事件查询、按权限掩码、过滤、详情和导出。
- 完成旧记录迁移脚本，但暂不删旧结构。

### 阶段 2：共享扫码服务

- 实现统一条码解析、事件 claim、fingerprint、锁顺序和错误码。
- 使用 `transaction.atomic()`；扫码单笔路径明确禁用提交后才 flush 的 `batch_tracking_entries()`，让 Tracking 在事务内直接保存。
- 插件事件与非关键后台任务按原生方式在提交后触发，不能影响库存事实原子性。
- 实现 Tracking 的 `scan_event` FK、过滤和详情关联。

### 阶段 3：重构扫码入库

- 目标库位显式确认。
- 取消特殊 StockItem 和强制 `SCAN-INBOUND` 批号。
- 普通件兼容聚合；未知码事务内建档并继续入库。
- 保留旧 API 响应兼容层。

### 阶段 4：实现扫码出库

- 来源库位显式确认。
- 商品码候选选择和精确非序列号 StockItem 码路径。
- 严格检查未分配数量和并发抢占。
- 增加不足、歧义、冻结、过期、未知码等失败日志。

### 阶段 5：前端、库存入口与日志

- 新扫码作业路由、模式防误操作和组件拆分。
- 复用 StockItemTable；扩展 StockTrackingTable 的 global/part/item 三种 scope。
- 添加扫码事件表、筛选、详情和导出。
- 明确原生库存数量 CRUD 入口和中文文案。
- 把旧 `ScanInbound.tsx` 现有中文源 msgid 一并迁移为英文，所有新文案继续使用 Lingui `t` 宏，在 `zh_Hans/messages.po` 人工填写准确中文；不手改生成的 `messages.ts` / `messages.d.ts`，由 extract/compile 生成。

### 阶段 6：迁移切换与观察

- 按第 12 节执行受控切换。
- 上线后对比库存总量和 Tracking/扫码事件一致性。
- 稳定一个发布周期后执行发布 B 清理。

## 14. 失败场景和产品反馈

| 场景 | 服务端结果 | 页面反馈 |
| --- | --- | --- |
| 未知码入库且有建档权限 | 建档、绑码、入库同事务 | 显示新建商品和入库成功 |
| 未知码出库 | 失败事件，数量 0 | 红色提示“商品未建档，不能出库” |
| 扫商品前未选择库位 | 不改库存，另写失败扫码事件 | 提示先扫描/选择库位 |
| 结构库位 | 失败事件 | 提示该库位不能存放库存 |
| 入库存在批次/属性歧义 | 不增加任何候选 | 显示批次、状态、包装、有效期，要求选择或新建 |
| 多个出库候选 | 不扣库存 | 打开候选选择，显示批次/数量/有效期 |
| 扫到序列号 Part/StockItem | 失败事件，数量 0 | 提示改走采购接收、销售发货或原生序列号流程 |
| 可用库存不足 | 失败事件，无 Tracking | 显示现有量、已分配量和可用量 |
| 同 UUID 重试 | 重放原结果 | 标记“已处理”，不重复播报库存增加/减少 |
| 同 UUID 不同请求 | HTTP 409 | 提示请求冲突并生成新业务事件后再提交 |
| 两人抢最后一件 | 一个成功、一个失败 | 失败端刷新实时库存 |
| 操作员被停用/删除 | 快照仍显示原姓名 | 可显示“账号已停用/已删除”但不丢姓名 |
| 事务中任一步失败 | 库存、Tracking、成功事件全回滚 | 显示可重试错误，不出现半成功 |

## 15. 测试计划

### 15.1 后端模型和权限

- 操作员只能来自 `request.user`，客户端伪造字段无效。
- 用户改 username、姓名或显示名设置后，旧快照不变化。
- 用户删除后 FK 为空、快照仍存在。
- 系统事件 `actor_kind` 正确，不能冒充人工操作。
- `operation`、`outcome`、正数量和 UUID 唯一约束生效。
- 删除 Part、StockItem、Location 后扫码快照仍可读取。
- view-only、add-only、change-only、add+change、建档用户和管理员直接调用 API 时符合矩阵；尤其 add-only 不能扫码出库。
- 未登录、无扫码权限、非法 UUID、空/超长条码和非正数量在领域服务前拒绝，不能产生正式 ScanStockEvent。

### 15.2 入库

- 已知商品只对唯一明确匹配的中性库存项准确 +1。
- 同库位存在不同批次、状态、包装、供应商或有效期候选时返回选择，不静默按主键聚合。
- 显式选择库存项后，锁内属性不匹配时拒绝。
- 无匹配项时只创建一个 `quantity=0, save(add_note=False)` 的普通 StockItem，再由 `STOCK_ADD` 增加数量；整次扫码恰好一条关联 Tracking。
- 未知码建 Part、绑码、建库存和日志同事务。
- `SETUP_REQUIRED` 预检不 claim UUID、不写最终事件；补齐建档资料后复用原 UUID 只执行一次入库。
- 快速建档权限不足时无 Part、无 IPN 注册、无库存副作用。
- 同 UUID 五次并发只增加 1；五个不同 UUID 增加 5。
- 两终端并发建同一未知码只生成一个 Part。
- 序列号 Part/StockItem 入库返回 `SERIALIZED_STOCK_REQUIRES_ORDER_FLOW`，库存和 Tracking 不变。

### 15.3 出库

- 精确非序列号 StockItem 码和唯一非序列号 Part 候选均可减少 1。
- 多库位、多批次返回歧义且不扣库存。
- `STOCK_SELECTION_REQUIRED` 预检不 claim UUID；选择候选后复用原 UUID，只有最终执行产生一条事件。
- 未知商品码绝不自动建档。
- 过期、冻结、待检、已安装、已交付库存不进入候选。
- 已分配数量不可被直接扫码出库。
- 加锁后库存不足时不生成成功事件和 Tracking。
- 两个事件抢最后一件只有一个成功，不出现负库存。
- 出库与 Sales Order 分配并发时不超卖。
- 出库与 Build/Transfer Order 分配、移库或批次编辑并发时，必须在同一锁顺序下重验，不能超卖、扣错库位或扣错批次。
- 序列号 Part/StockItem 出库返回 `SERIALIZED_STOCK_REQUIRES_ORDER_FLOW`，不能通过 Count=0 或删除绕过。

### 15.4 日志和幂等

- 每次成功扫码恰好一条 ScanStockEvent 和一条相关 Tracking。
- 失败扫码只有失败事件，没有库存 Tracking。
- 强制模拟 Tracking 创建失败时，库存数量和成功 ScanStockEvent 必须同时回滚。
- 成功重放不增加库存、不新增两种日志。
- 同 UUID 换用户、方向、码、库位、批次或数量全部返回 409。
- 同 UUID 并发业务失败只提交一条 FAILED；一条请求业务失败时，竞争请求等待后只能重放该失败，不能抢先执行库存变化。
- 迁移后的 `legacy-v0` 成功 UUID 按旧 barcode + operator 契约重放；即使默认库位后来变化，也不重复改库存、不误报新 fingerprint 冲突。
- 操作员 ID、用户名、显示名、时间、数量 delta 和 before/after 正确。
- 原生 Add/Remove/Count/Move 以及采购、销售、构建等批量 Tracking 路径，只要存在人工 `user`，三个操作员快照都不能为空。
- Tracking 写 API、导入器和客户端 payload 不能覆盖操作员快照或 `scan_event`；伪造值必须被忽略或拒绝。
- 列表筛选、分页、掩码、详情权限和 CSV/XLSX 导出正确。
- 手工 Add/Remove/Count 只生成原生 Tracking，不伪造扫码事件。
- Tracking 通过真实 `scan_event` FK 精确关联；Tracking 被保留策略清理后，ScanStockEvent 仍能独立展示完整审计快照。
- 不同 tracking type 的 `deltas.quantity` 按各自语义显示；Move 新记录显示 old_location → location，旧记录不伪造来源。

并发用例必须使用 Django `TransactionTestCase`，并至少在与生产同类的 PostgreSQL 数据库执行一次；SQLite 可能忽略 `select_for_update()`，不能作为锁正确性的唯一证据。

### 15.5 迁移

- `ScanStockEvent.objects.filter(legacy_inbound_record_pk__isnull=False).count()` 等于旧 `ScanInboundRecord` 数，legacy pk/UUID 集合一一对应；总事件数允许等于 legacy 数 + 切换后新事件数。
- 每条旧 UUID、barcode、quantity、time、user 一致，关键字段 checksum 相符。
- 所有旧 StockItem 主键、数量、库位、批次迁移前后不变。
- Part 总库存逐一相等，总库存汇总相等。
- 发布 A 后旧 flag 数量不变、新服务不再产生新 flag；发布 B 清除 flag 后旧项可正常入库、出库、移库、拆分和合并。
- 迁移可在中断后安全重跑，不重复事件。
- 核对失败时停止切换，旧表和字段仍可恢复。
- 切换后只回滚应用但不回滚数据库的演练必须被阻止；完整备份恢复演练可回到旧写版本且不产生双轨库存项。

### 15.6 前端端到端

- 入库/出库路由、模式切换和清空上下文。
- 旧 `/stock/scan-inbound/` 使用 `<Navigate replace>` 落到新入库路由，页面只挂载一个扫码状态机。
- 扫库位、扫已知码、未知码建档、候选选择、连续扫描。
- 加载、空数据、失败、离线/超时和重放状态。
- 当前库存表的原生 Add/Remove/Count 与刷新。
- 两类日志、操作员、筛选、跳转和导出。
- 全局库存流水明确显示 Part/IPN/StockItem，用户 FK 为空时回退到操作员快照。
- 无权限时路由、按钮和 API 均不可用。
- 原生库存项详情“扫描库位并移库”仍正常，且不改变数量。
- 英文 locale 的扫码页不残留中文源文案；`zh_Hans` 精确显示“扫码作业 / 新建库存项 / 添加库存 / 移除库存 / 盘点库存 / 扫描库位并移库”。

建议命令（均从 InvenTree 仓库根目录执行）：

```bash
invoke dev.test --keepdb --runtest=stock.test_api
invoke dev.test --migrations
cd src/frontend && npm run extract
cd src/frontend && npm run compile
cd src/frontend && npm run build
cd src/frontend && npx playwright test tests/pages/pui_scan_stock.spec.ts
```

## 16. 验收标准

1. 扫已知非序列号商品码入库后，正确库位中唯一明确匹配的 StockItem 恰好 +1；有批次/属性歧义时不改库存并要求选择。
2. 未知码入库可在权限允许时建 Part、绑定商品码并完成首次入库；Part 与 StockItem 各有自己的原生二维码。
3. 扫码出库从显式来源库位的非序列号可用库存恰好 -1；序列号、未知码和不足库存绝不产生副作用。
4. 同一事件 UUID 网络重试不会重复增减库存。
5. 不再为新扫码创建特殊 `is_scan_inbound_item` 库存项或强制 `SCAN-INBOUND` 批次。
6. 原生库存 Add/Remove/Count/Transfer 全部仍可用，数量 CRUD 入口和中文名称清楚。
7. 库存详情原“扫码入库”已改名“扫描库位并移库”，行为仍是移库而非加库存。
8. 每条成功扫码通过 FK 关联对应库存事实；Tracking 在保留期内可跳转，被原生策略清理后扫码事件仍保留完整快照；失败扫码能看到稳定原因。
9. 两类日志都显示操作员；新记录在用户改名、停用或删除后仍保留操作时快照，且 JSON/导出与 UI 一致。
10. 迁移前后每个 Part 和 StockItem 的库存数量一致。
11. 并发抢最后一件、重复扫码、未知码并发建档均不产生负库存或重复 Part。
12. 相关后端、迁移、前端构建、Lingui 和 Playwright 测试全部通过。
13. add-only 用户不能调用扫码出库；已有库存调整必须由 change 权限控制。

## 17. 上线与回滚

### 上线

- 发布 A 先部署兼容模型和只读能力。
- 在生产数据副本跑完整迁移和数量核对。
- 选择短暂停写窗口执行数据切换。
- 先小范围开放扫码入库，再开放扫码出库。
- 观察 API 失败率、幂等冲突、库存不足、候选歧义和日志写入失败。

### 回滚

- 首次切换不删除旧表和旧字段。
- 新服务异常时先关闭入/出库写入口并保持查询可用，优先前向修复。
- 切换后已经可能产生普通 StockItem 和新事件，因此禁止只切路由恢复旧写 serializer；旧逻辑会重新创建特殊库存项并造成双轨数据。
- 如必须回到旧写版本，应用版本和数据库必须一起恢复到切换前备份；不能只回滚代码。若不恢复数据库，旧扫码端点只能保持只读。
- 回滚前按事件 UUID 识别新服务已经成功执行的操作，禁止简单重放旧请求。
- 数据核对失败时保持停写，使用数据库备份和迁移报告恢复。
- 只有稳定一个发布周期后才允许发布 B 删除旧结构。

## 18. 预计涉及文件

后端重点：

- `src/backend/InvenTree/stock/models.py`
- `src/backend/InvenTree/stock/serializers.py`
- `src/backend/InvenTree/stock/api.py`
- `src/backend/InvenTree/stock/test_api.py`
- `src/backend/InvenTree/stock/migrations/`
- `src/backend/InvenTree/order/models.py`（批量 Tracking 操作员快照）
- `src/backend/InvenTree/build/models.py`（批量 Tracking 操作员快照）
- `src/backend/InvenTree/users/models.py`（复用 canonical display resolver）
- `src/backend/InvenTree/importer/`（审计字段禁止导入伪造）

前端重点：

- `src/frontend/src/pages/stock/ScanInbound.tsx`（拆分/替换）
- `src/frontend/src/pages/stock/StockDetail.tsx`
- `src/frontend/src/tables/stock/StockItemTable.tsx`
- `src/frontend/src/tables/stock/StockTrackingTable.tsx`
- `src/frontend/src/components/nav/NavigationDrawer.tsx`
- `src/frontend/src/router.tsx`
- `src/frontend/lib/enums/ApiEndpoints.tsx`
- `src/frontend/src/locales/`
- `src/frontend/tests/pages/pui_scan_stock.spec.ts`

## 19. 明确不在本次范围

- 重写 InvenTree 整套库存 CRUD。
- 用扫码事件表替代 `StockItemTracking`。
- 把商品 IPN 改绑成某个 StockItem 的内部码。
- 把普通查看页面和搜索行为全部做成用户行为审计。
- 自动建立销售订单、采购订单或完整 ERP/MES 流程。
- 离线扫码队列和跨终端离线同步。
- 首次迁移时自动合并历史库存项或清洗历史批次。
- 尚未确认的 FEFO/FIFO 自动出库策略。

## 20. 已确认的业务选择

初版“扫码出库”作为**无单据的直接库存扣减**，正式销售出货继续走 InvenTree 原生 Sales Order。

已按此选择实施。将来如需“扫码即生成/履约销售单”，应作为第二阶段单独设计。
