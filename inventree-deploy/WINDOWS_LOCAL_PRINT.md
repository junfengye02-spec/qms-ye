# Windows 本地标签打印：开发交付与部署手册

> 2026-09-20 后续部署更新：已按用户要求完成内网测试实例部署和服务检查，等待 Windows 实机准备/验收。详见 [Windows 测试交接](../docs/WINDOWS_LOCAL_PRINT_READY_FOR_WINDOWS.md)。下文“尚未部署”描述为此前开发验收时的状态。

2026-09-20。当前为本机开发版本，默认关闭；尚无 Windows、驱动、可信证书、出纸或扫码验收。用户已授权先在 macOS 本机开发和模拟测试，阶段一硬件门槛改为上线前门槛。

## 用户流程

启用后，商品和库存的单件、列表多选“打印标签”打开“本机标签打印”。模板固定为商品 IPN 标签、50 × 30 mm。报告、库位、制造及功能关闭时沿用原入口。

1. Windows 安装厂商驱动，先成功打印系统测试页。
2. 在 WMS 点“安装打印组件”。只有管理员配置了经过验证的 HTTPS 下载地址时才提供安装包链接；否则显示尚未配置。网页不能自动完成 Windows 安装或信任。
3. 安装指定版本 QZ Tray，允许当前登录用户会话登录自启。按管理员的证书方案完成本站信任；不要关闭浏览器、HTTPS 或组件安全校验。
4. 返回 WMS 点“重新检测”，明确选择打印机。选择只保存在当前浏览器、本站配置下，不跟随账号同步；每次刷新和提交前检查队列仍然存在，不自动换打印机。
5. 填每件份数，核对总张数后点打印。正常路径不会打开 PDF。结果“已提交到本机打印队列”不是出纸证明。
6. 如组件未安装，可主动“下载 PDF”。PDF-only 任务不发送到设备；需要本机打印时点“新建标签任务”并选择实际队列。

失败时查看“最近任务”。生成完成且从未签发发送签名的任务可以明确继续提交。签发过签名的任务不自动重发：超时或丢失回执显示“结果待确认”。检查 Windows 队列和已出标签后，勾选确认再创建补打任务；补打会保留原任务关联。

创建请求响应丢失时，弹窗保留请求去重键并提供“查询上次请求”，查询不会自动发送。刷新后从最近任务恢复同样不会自动发送。退出登录或切换用户会中断尚未发送的流程；已交给系统队列的任务需要在 Windows 处理。

## 服务端部署

构建并部署本次前后端代码，先在备份数据库上执行迁移验证；新增 `report.0033_localprintjob` 只增加审计表，不修改旧打印数据。正式应用迁移前备份数据库。旧应用可忽略新表，回退时保留表和历史记录。

```sh
docker compose exec inventree-server python manage.py migrate --noinput
docker compose exec inventree-server python manage.py configure_local_print_templates
```

模板命令默认预览。它按模型类型识别模板，不依赖 ID 7、8；缺失、重复、禁用（运行时）、文件丢失和尺寸错误均拒绝。应用配置会启用两条 Product IPN 绑定，只按“名称完全相等 + 模型类型”停用计划中三个示例，保留自定义模板、库位和制造标签及所有文件。命令不会删除模板，也不会修改内容。

```sh
# 在受控配置窗口执行，backup 必须是不存在的新文件，目录需预先创建。
docker compose exec inventree-server python manage.py configure_local_print_templates \
  --apply --backup /home/inventree/data/local-print-labels-before.json

# 显式恢复：先预览，再应用。身份、文件摘要、尺寸改变时拒绝恢复。
docker compose exec inventree-server python manage.py configure_local_print_templates \
  --restore /home/inventree/data/local-print-labels-before.json
docker compose exec inventree-server python manage.py configure_local_print_templates \
  --restore /home/inventree/data/local-print-labels-before.json --apply
```

重复配置时使用新的备份文件，不覆盖首次备份。备份只记录启用标志和身份/内容摘要；它不替代数据库和媒体备份。组件回退不需要恢复旧示例模板。

### 配置项

| 环境变量 | 默认值/用途 |
| --- | --- |
| `INVENTREE_LOCAL_PRINT_ENABLED` | `False`；关闭时创建/签名被拒绝，旧打印入口保留 |
| `INVENTREE_LOCAL_PRINT_QZ_VERSION` | `2.2.5`；当前候选组件版本，必须精确匹配，尚未经 Windows 验证 |
| `INVENTREE_LOCAL_PRINT_DOWNLOAD_URL` | 空；管理员验证后的 HTTPS 安装包地址 |
| `INVENTREE_LOCAL_PRINT_CERTIFICATE` | 空；签名服务内的 X.509 PEM 公钥证书文件路径 |
| `INVENTREE_LOCAL_PRINT_PRIVATE_KEY` | 空；签名服务内的 PKCS#8 PEM RSA 私钥文件路径 |
| `INVENTREE_DATA_UPLOAD_MAX_MEMORY_SIZE` | 功能启用时默认 12 MiB；容纳完整校验请求，反向代理应匹配 |

JS SDK 固定 `qz-tray@2.2.5`，依赖包声明 LGPL-2.1。这是开发协议基线，不是已验证的 Windows 支持声明；较新的 QZ 版本需要先通过协议及实机测试再改精确版本配置。依赖更新不能仅修改下载链接。

签名证书需已被目标 QZ 安装信任。生产信任方案、分发许可、实际费用及维护责任仍待确认，不将自动化测试临时生成的自签名证书用于生产。

准备好文件后可选用 `docker-compose.local-print.yml`。将以下值放入部署环境（不要把私钥内容写入环境变量）：

```dotenv
INVENTREE_LOCAL_PRINT_ENABLED=False
INVENTREE_LOCAL_PRINT_QZ_VERSION=2.2.5
INVENTREE_LOCAL_PRINT_DOWNLOAD_URL=
WMS_LOCAL_PRINT_CERTIFICATE_FILE=/absolute/private-directory/digital-certificate.txt
WMS_LOCAL_PRINT_PRIVATE_KEY_FILE=/absolute/private-directory/private-key.pem
```

两个文件以 Docker secrets 只挂载进实际签名的 `inventree-server`。渲染 worker 不需要私钥。宿主机目录需限制访问、私钥建议 `0600`，确认容器服务用户可读；不可存入媒体目录、静态文件目录或仓库。空文件/不匹配密钥/过期证书会拒绝签名。该 overlay 未应用到现有运行实例。

完成指定工位真机验收后将开关置为 `True`，再用原 compose 文件加 overlay 重建 server 以加载配置。保持 worker 运行，以复用原 PDF 渲染任务。

### 签名与任务协议

API 前缀 `/api/label/local/`，全部需要登录，写操作沿用会话认证的 CSRF 校验及项目权限机制：

| 路径 | 方法/用途 |
| --- | --- |
| `config/` | GET 功能开关、候选版本、安装链接、最大张数 |
| `certificate/` | GET 仅公钥证书，校验证书有效期及与私钥匹配 |
| `jobs/` | POST 创建去重任务；GET 当前用户最近 20 个任务 |
| `jobs/<uuid>/` | GET 状态，包括保守超时处理 |
| `jobs/<uuid>/pdf/` | GET 已授权 PDF 的 base64、摘要及固定纸张参数 |
| `sign/` | POST 完整消息；打印必须额外指定任务 UUID |
| `jobs/<uuid>/result/` | POST `submitted` 或 `uncertain`，均为客户端观察 |
| `jobs/<uuid>/reprint/` | POST 新去重键，保留原商品/库存及份数，新建关联任务 |

创建输入：UUID `request_key`、`model_type`（part/stockitem）、唯一 `items` ID 数组、`copies`、精确 `queue`。每次打印最多 500 张。后端生成 DataOutput PDF，复用现有模板/IPN/份数校验；另存 LocalPrintJob 记录队列、输出摘要、原始对象 ID、模板、操作者、状态和时间。PDF 上限 8 MiB，逐页验证 50 × 30 mm 和总页数；超限应拆分批次。下载仅经登录网页，组件不接收 Cookie 或媒体 URL。

签名只允许空查询的 `printers.find`，或完全匹配任务文件、队列及全部固定参数的 `print`。不允许 raw、文件/网络/串口操作或额外参数。完整消息使用 30 秒时间窗口，任务有效期 15 分钟；签发打印签名时原子地从 ready 转为 submitting，每个任务只签发一次。60 秒未确认后查询会转为 uncertain。后台 PDF 生成失败、worker 未执行、任务过期均不触发设备重试。

前端先取得服务端批准的完整消息签名，再只允许 QZ 回调消费匹配的 SHA256 摘要一次。使用 SDK 回调路径附加 `SHA512`，避免 QZ 2.2.5 的预签名快捷路径遗漏算法字段。实际 RSA 校验和 SDK 报文均有自动化测试。

数据库与打印机不能组成事务，不能保证物理“恰好一次”。服务端防止正常客户端跨标签页重复认领，但不是打印机端的重放防护；已授权的签名也不能撤回已经进入设备的命令。未知结果需人工确认。

## 故障处理与证书维护

- 无法枚举：检查 Windows 测试页、用户是否登录、QZ 托盘、指定版本、浏览器本地网络访问许可、本机 WSS 证书和 WMS HTTPS 证书。不得改为不安全 ws。
- 证书/信任拒绝：区分网站 HTTPS、本机 WSS、打印签名三类证书；检查服务端挂载路径、有效期、证书/私钥是否配对，以及 QZ 本站信任。
- 缺纸/暂停/脱机：此版本没有硬件回执，也没有声称获取实时缺纸状态；检查 Windows 系统队列和打印机面板。
- PDF 失败：检查 IPN、模板启用与尺寸、队列 worker 和服务日志。生成中的任务超期后为失败，可以显式补打；不自动重新入队。
- 结果待确认：按任务 UUID 对应 `WMS-<uuid>` 系统队列名称核对，不通过刷新重复发送。
- 证书维护：指定管理员在证书到期前 30 天、7 天检查并安排更新（此处是运维要求，未创建提醒自动化）。轮换前关闭功能，安全替换文件，重建 server，更新必要的客户端信任并复测；确认通过再开启。
- 泄露/撤销：立即关功能停止签名、撤销发行方凭据或移除内部 QZ 信任、更换密钥并检查任务记录。不要恢复旧私钥或自动重放历史任务。

## 回退

置 `INVENTREE_LOCAL_PRINT_ENABLED=False` 并重建 server，重新加载网页恢复原 PDF 打印入口。即使旧标签页还开着，服务端也会拒绝新的创建/签名。保留任务记录和新表，不逆迁移删表；必要时恢复旧应用版本，旧应用无需读取新增表。已签发/已发送的任务仍按 Windows 队列核实。保留客户端安装不会影响手动 PDF 使用。

## 上线前必须补齐

Windows 版本/CPU/浏览器、机型/连接方式/DPI/标签纸；驱动和组件安装来源/校验值；HTTPS/WSS/签名信任部署；新机安装、登录自启、重启/锁屏/注销；两台机器的队列隔离；1/3/6 张实物；中文/尺寸/方向/二维码 IPN；实际入出库扫码；缺纸/断网/改名；一个工位完整班次试点；签名分发许可和维护责任。无上述证据不宣布上线。

参考：[QZ 签名文档](https://qz.io/docs/signing)、[打印配置](https://qz.io/api/qz.configs)、[Windows 本地标签打印执行计划](../docs/WINDOWS_LOCAL_PRINT_EXECUTION_PLAN.md)。
