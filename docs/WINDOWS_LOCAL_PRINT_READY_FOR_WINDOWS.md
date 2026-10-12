# Windows 实机测试交接（2026-09-20）

状态：服务端与内网准备完成，等待 Windows 工位安装和实机验收。未执行出纸、扫码或整班次试点。

## 测试入口

- WMS： https://172.16.130.22:18443/
- 安装与信任准备页： https://172.16.130.22:18443/local-print-setup/
- 公钥准备包： https://172.16.130.22:18443/local-print-setup/windows-print-test-kit.zip
- 首次网站 HTTPS 信任所需 CA： http://172.16.130.22:18000/wms-ca.crt

Windows 必须能连接本机当前网络 IP。2026-09-21 当前 en0 地址为 172.16.130.22，昨日 10.49.173.98 已失效。
使用现有测试账号；本次没有重置密码或新建管理员。首次下载 CA 时请与管理员核对证书指纹，导入当前用户受信任根证书，再以 HTTPS 访问。

## 已完成

- 仅更新 wms-preview、wms-preview-worker、wms-preview-proxy 测试容器。
- 测试数据库 wms_inventory_acceptance 已备份，report.0033_localprintjob 已迁移。
- Product IPN 的商品/库存绑定启用，三个旧示例停用；模板变更另有备份。
- 新版前端独立构建挂载，页面实际引用及返回的 JS 字节与构建产物一致。
- 本地打印开关 True；组件精确版本 2.2.5；安装入口指向准备页。
- 生成独立 RSA 3072 位测试签名身份，私钥位于仓库外，权限 0600，只挂载到测试 web。
- HTTPS 验证通过；系统检查无问题；登录后配置接口、真实 RSA SHA512 签名、真实 worker 生成一页 50 × 30 mm PDF、同键请求去重均通过。
- 未登录访问返回 401；检查使用的临时登录会话已删除。
- PDF 检查任务 4b813065-9e86-4154-a88b-4ba18be3c3a1，队列名 DEPLOYMENT-CHECK-NO-DEVICE；没有签发打印发送命令，没有连接设备。

测试签名证书到期：2026-10-20 08:57:56 UTC（北京时间 16:57:56）。
指纹 SHA256：9B:04:64:6D:E3:21:67:FF:03:8B:F5:2F:01:2B:E8:EA:D9:04:64:24:92:8F:57:24:5F:F1:09:42:53:C8:D1:E0。
这是测试身份，生产信任方案仍需另行确定。

## Windows 上剩余操作

1. 按机型安装驱动，设置 50 × 30 mm，打印系统测试页。
2. 从准备页选择 x64 或 ARM64 官方 QZ Tray 2.2.5 安装包，核对页面 SHA256。
3. 解压准备包，按 README 配置网站 CA 与 QZ 测试签名信任。已有 QZ 自定义配置需合并，不能覆盖。
4. 正常用户启动 QZ 并开启自启，登录 WMS，在商品/库存“打印标签”里重新检测并明确选择队列。
5. 开始 1/3/6 张、方向/边距/中文/二维码、补打和故障恢复验收；“已提交”不等于已出纸。

Windows 脚本/信任步骤尚未在真机执行。准备包不包含私钥。安装包使用官方外链，没有在本机下载或执行。

## 运维与回退

数据库及模板备份：`/Users/jf/.wms-local-print-preview/backup-20260920-165741`。
测试签名文件：`/Users/jf/.wms-local-print-preview/`。
部署日志与检查脚本：`tmp/windows-local-print-deploy/`。
运行前端挂载：`tmp/local-print-build/`，测试期间保留此目录。
回退容器：`wms-preview-before-local-print-20260920165914`（已迁移后的旧功能关闭配置）。

需要关闭新功能时停止当前 wms-preview 并改名保留，再将上述旧容器改回 wms-preview 并启动；随后重载测试代理，浏览器刷新。保留数据库新增表、任务记录以及现有模板备份，不逆迁移删除数据。

部署脚本：inventree-deploy/scripts/enable-preview-local-print.py。它会明确重新启用功能；仅在准备好证书、构建和测试数据库时使用。

官方依据：[QZ 2.2.5 版本与安装包](https://github.com/qzind/tray/releases/tag/v2.2.5)、[自签名 CA provisioning](https://qz.io/docs/provisioning)、[命令行 certgen](https://qz.io/docs/command-line)。

网站 HTTPS CA SHA256 指纹：A0:5A:A4:26:1A:F9:0A:0F:B8:77:A9:D9:0D:DD:20:37:E8:D2:F1:DC:06:6F:D5:66:C5:CA:C5:A9:A2:CA:16:C8。

## 2026-09-21 签名身份更新

当前启用 identity-2126，证书到期 2126-09-21，公钥指纹 11:E3:BB:07:DB:49:73:1A:23:83:78:02:60:16:8E:DE:A5:8C:2D:EE:28:9E:15:24:4C:33:A4:4E:87:7C:E9:D1。服务端已验证密钥配对、有效期及 RSA SHA512 签名；Windows 需更新信任后复测。100 年是本次设置期限，不是 QZ 官方兼容期限保证。

旧签名身份保留在 ~/.wms-local-print-preview 根目录及 certificate-archive-20260921-100830；旧演示包位于准备页 legacy-20261020/windows-print-test-kit.zip。新版通过 active 符号链接选择 identity-2126，并只读挂载到测试 web；生产实例未切换。回退可将 active 指向根目录的旧身份目录（或归档目录）再执行部署脚本，Windows 同步恢复旧包；旧证书仍于 2026-10-20 到期。新旧不能同时用于同一签名端点。

准备页已改为“Windows 打印组件安装”，新包、安装更新步骤和折叠旧版入口均已部署。
