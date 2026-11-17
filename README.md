# fnssh — 飞牛 fnOS 极简 SSH 终端

一个为 **飞牛 fnOS** 打造的极简 Web SSH 终端应用（`.fpk` 安装包）。
安装时配置一次目标主机、账号密码和连接后自动执行的命令，之后**打开即自动连接**，直接进入全屏终端——没有多余界面，没有多余信息。

> 参考开源项目 [cmliu/webssh](https://github.com/cmliu/webssh) 的架构思路，按 [飞牛官方开发文档](https://developer.fnnas.com/docs/guide/) 规范打包。

## 特性

- **极致简洁**：安装向导填 5 项（主机地址、SSH 端口、用户名、密码、连接后自动执行的命令），打开应用即全屏终端
- **自动连接**：自动认证（密码经 pty 自动输入）、自动执行配置的命令（如 `sudo -i`）
- **窗口内多标签多开**：右上角 `+` 新建独立 SSH 会话标签，可同时开多个会话；单会话时标签栏自动隐藏
- **接入飞牛统一网关**：走 fnOS 登录态与域名路径（`/app/fnssh/`），**飞牛 App / FN Connect 远程可访问**，不受局域网限制
- **零第三方依赖**：后端纯 Python 标准库（自实现 RFC6455 WebSocket），SSH 使用系统 `openssh-client`，无需安装任何 pip 包，安装包仅约 92KB
- **随时改配置**：应用「运行设置」中修改后保存即生效，无需重启
- **深色图标**：纯黑背景 + 白色 `>_` 提示符

## 架构

```
浏览器 (xterm.js, 本地静态资源)
   │  WebSocket (RFC6455)
   ▼
Python 后端 (纯标准库 http.server + 自实现 WS)
   │  pty 桥接 (pty.fork)
   ▼
系统 ssh 命令 (openssh-client) ──► 目标主机 SSH
```

- 服务端同时监听 **Unix Socket**（`$TRIM_APPDEST/fnssh.sock`，供统一网关转发）和 **TCP 8899**（直连兜底）
- 前端 xterm.js 三件套内置在包内，无 CDN 依赖

## 安装

1. 将 `fnssh.fpk` 拷贝到 fnOS 设备
2. 应用中心 → 设置 → 手动安装，导入 fpk
3. 安装向导中填写：主机地址 / SSH 端口 / 用户名 / 密码 / 连接后自动执行的命令
4. 桌面出现「SSH」图标，点击即用

> 升级提示：若覆盖安装后页面无变化，清一次浏览器缓存即可（页面缓存 24h）。

## 配置存储

- 向导值写入 `/vol1/@appconf/fnssh/config.json`（权限 600，密码明文存储于 NAS 本机）
- 服务端每次建立连接时读取，改配置即时生效
- 诊断日志：`/vol1/@appdata/fnssh/install.log`、`server.log`

## 从源码打包

```bash
cd fnssh
fnpack build   # 生成 fnssh.fpk
```

目录结构：

```
fnssh/
├── manifest            # 应用元数据（名称/版本/端口/入口）
├── ICON.PNG / ICON_256.PNG
├── config/
│   ├── privilege       # 以 root 运行
│   └── resource        # service_port=8899
├── wizard/
│   ├── install         # 安装向导表单（host/port/user/password/command）
│   └── config          # 运行设置表单（同上，可随时改）
├── cmd/                # 生命周期脚本（install_init/install_callback/main/...）
└── app/
    ├── server.py       # 后端：WS 服务 + ssh pty 桥接（纯标准库）
    ├── write_config.py # 写配置（fnOS 只部署 cmd/ 标准脚本，自定义 cmd 脚本不落地）
    ├── ui/             # 桌面入口 config（gatewaySocket + gatewayPrefix）与图标
    └── www/            # 前端：全屏 xterm.js 多标签终端
```

## 常见问题

| 现象 | 处理 |
|---|---|
| 安装报「执行脚本出错」 | 看 `/vol1/@appdata/fnssh/install.log`，日志会写明具体环节 |
| 终端第一行不是你配置的地址 | 说明 config.json 未生成，到应用「运行设置」重新保存一次 |
| 页面升级后没变化 | 浏览器缓存（24h），清缓存刷新 |
| 8899 端口被占用 | 不影响网关通道使用；如需换端口改 manifest 的 service_port |
| 安全提示 | 统一网关走 fnOS 登录态；8899 直连端口无鉴权，建议仅内网使用 |

## 版本要点

- **v1.1.0** 去除 tornado/paramiko 依赖，纯标准库重写（12MB → 92KB）
- **v1.1.2** 修复配置未写入（fnOS 只部署 cmd/ 标准生命周期脚本，写配置逻辑移入 app/）
- **v1.2.0** 接入飞牛统一网关，飞牛 App 远程可访问
- **v1.2.2** 多开改为窗口内多标签
- **v1.2.5** 标签栏智能显隐（单会话隐藏，防全部关闭）

## 许可

仅供学习与个人使用。
