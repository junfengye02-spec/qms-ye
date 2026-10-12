# 手机 HTTPS 扫码入口

当前局域网地址：**https://172.16.130.22:18443/web/stock/scan/inbound/**

出库页面：**https://172.16.130.22:18443/web/stock/scan/outbound/**

电脑原有 `http://localhost:18000` 入口保留。

## 安卓手机使用

1. 手机与服务器电脑连接可互通的局域网。
2. 如果尚未信任本系统 CA，在手机上下载：
   http://172.16.130.22:18000/wms-ca.crt
3. 在手机“设置”中搜索“安装证书”或“CA 证书”，安装刚下载的 `wms-ca.crt`。不同品牌的菜单位置可能不同。
4. 用 Chrome 打开上面的 HTTPS 地址，登录后打开扫码摄像头，允许摄像头权限。

这次继续使用原来的 Caddy CA；如果手机已安装并信任相同证书，无需重复安装。
证书主体：`Caddy Local Authority - 2026 ECC Root`。
有效期：2026-09-03 至 2036-07-12。
SHA-256 指纹：
`A0:5A:A4:26:1A:F9:0A:0F:B8:77:A9:D9:0D:DD:20:37:E8:D2:F1:DC:06:6F:D5:66:C5:CA:C5:A9:A2:CA:16:C8`

安装方法参考：[Google 安卓证书说明](https://support.google.com/pixelphone/answer/2844832)。
[摄像头 API 要求安全上下文](https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getUserMedia)。

## 部署配置

- `wms-preview-proxy`：保留 `18000:8000`，新增 `18443:8443`。
- `Caddyfile.preview` 同时提供 HTTP 与 HTTPS，HTTPS 使用 `tls internal`。
- 与现有 Caddy 共用持久化证书存储 `inventree-data/caddy`，自动续期，重建容器不会更换 CA。
- 公开下载目录 `certificates/public` 仅包含根证书公钥文件，不包含私钥。
- web / worker 继续使用 `wms_inventory_acceptance` 和 Redis DB 1；`SITE_URL` 改为 HTTPS，补齐可信来源，并开启 `X-Forwarded-Proto` 识别。
- 原 443 端口服务保持原配置。

[Caddy 文档：相同存储的实例会共享资源并协调证书管理](https://caddyserver.com/docs/automatic-https)。

部署或局域网 IP 变更后运行（参数为当前 IP）：

```sh
python3 inventree-deploy/scripts/enable-preview-https.py 172.16.130.22
```

脚本先校验 Caddy 配置，保留被替换的旧容器，发生替换错误时恢复原容器。
如果先使用旧脚本重新创建了预览服务，再运行本 HTTPS 脚本以补齐 HTTPS 挂载和端口。
IP 变化后，手机应使用新地址；CA 保持相同。

## 2026-09-10 验证

- 使用导出的 CA 严格验证 HTTPS，返回 HTTP 200，`ssl_verify_result=0`，没有跳过证书检查。
- HTTP 原入口 HTTP 200；CA 下载与原根证书字节一致。
- 操作员、审批员均通过真实 HTTPS 会话与 CSRF 登录，并能查看当前 237 条库存记录。
- 扫码页面及前端静态资源 HTTP 200。
- 操作员凭登录会话下载标签 PDF 成功；匿名下载返回 401。
- 后台 worker 在正确的预览数据库与独立缓存中正常运行。

验证脚本：`tmp/https-check/verify.py`。没有操作用户手机；手机的 CA 安装、浏览器信任和实际摄像头权限需在手机端完成。
