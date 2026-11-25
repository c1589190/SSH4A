# Proposal

## Why

Codex 和其他 Agent 需要按名称连接工作服务器，同时避免在日常命令、会话和工具输出里反复传递实际密码。ssh4a 的首版把用户连接配置、远程执行、交互式 Shell 和文件传输接入本地 CLI 和共享 MCP 服务。

## What Changes

- 在 Ubuntu 上交付可安装的 `ssh4a` 命令，产品版本为 `0.0.1-alpha`。
- 支持用户级连接配置的新增、查看、修改和删除，密码可选保存并加密。人可以无回显录入，外部程序可以通过标准输入无头录入。
- 提供一键登记入口，输入地址、用户名和密码后直接测试登录。通过后保存配置和主机信任，输出可复用的连接别名。
- 支持独立引用主机、用户名和密码，允许跨配置组合。可显式查询任一字段的实际值，包括密码。
- 通过同一连接入口提供无头命令执行、SFTP 单文件上传和下载，以及具备 PTY 的远程 Shell。
- 支持 Agent 持续向同一交互式会话发送键盘输入，并能按密码引用向该会话的远程提示发送密码。给人在终端提供轻量的配置选择入口。
- 提供可开机自启的用户级 MCP 服务，供同机多个 Agent 接入并调用 SSH 工具。MCP 令牌独立于 SSH 服务器配置和密码，支持按 Agent 签发和撤销。
- 配套安装、配置、Agent 调用、权限边界和本地验收文档。

## Capabilities

### New Capabilities

- `connection-profiles` — 用户级配置、加密凭据、字段引用和实际值查询。
- `remote-command` — 主机身份校验、使用解析后的字段连接以及无头执行。
- `interactive-shell` — 持续的 SSH PTY、显式发送密码引用和配置选择界面。
- `file-transfer` — 使用相同配置上传和下载单个文件。
- `mcp-service` — 用户级开机启动、Agent 接入鉴权、SSH 工具和多客户端会话隔离。

### Modified Capabilities

## Impact

这是空项目的首个本地交付。采用 Python 3.12、Paramiko、cryptography 和官方 MCP Python SDK，目标环境为当前 Ubuntu。CLI 和 MCP 共享配置解析、主机校验和 SSH/SFTP 实现。常驻服务由当前用户的 systemd 管理，验收使用隔离的 OpenSSH 实例、两个 MCP 客户端和临时配置目录。
