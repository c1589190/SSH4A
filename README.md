# ssh4a

面向 Agent 和本地脚本的 SSH 客户端。通过 CLI 或本机 MCP，使用已保存的连接执行命令、操作交互式 Shell 和传输文件。首版为 `0.0.1-alpha`，运行基线是 Ubuntu 24.04 和 Python 3.12。

## 快速开始

需要本机安装 Git 和 `uv`。

```sh
git clone https://github.com/c1589190/SSH4A.git
cd SSH4A
uv tool install --python 3.12 .
ssh4a --version
```

将下面的 IP、端口和用户名替换成实际信息。一条命令会无回显读取密码，立即测试 SSH 登录，成功后保存加密密码和连接配置。

```sh
ssh4a 192.0.2.10:2222 deploy --name work
ssh4a exec --profile work -- 'id -un && hostname'
```

省略端口时使用 `22`。省略 `--name` 时，ssh4a 输出自动生成的别名。首次一键添加会使用当前 SSH 握手返回的主机密钥，并在登录成功后保存。已有主机密钥不匹配时会中止。需要预先验证服务器身份时，使用下面的[指纹登记方式](#登记可信主机指纹)。

## 连接配置

### 添加和管理

```sh
ssh4a add server.example.org deploy --port 2222 --name work
ssh4a '[2001:db8::10]:2222' deploy --name ipv6
ssh4a list
ssh4a list --json
ssh4a profile show work
```

`ssh4a list` 按别名列出当前配置目录中的全部连接，显示主机、端口、用户名和是否保存了密码。脚本和 Agent 可用 `ssh4a list --json`，输出与 `ssh4a profile list` 相同。没有配置时，表格模式会显示提示，JSON 模式返回 `[]`。

这两个添加示例可以独立使用。已有别名用 `profile update` 修改，用 `profile delete NAME` 删除。外部程序可通过标准输入提供密码，ssh4a 会保留密码中的空白，并移除至多一个末尾 LF 或 CRLF。以下命令中的 `trusted-password-provider` 代表调用方自己的凭据来源。

```sh
trusted-password-provider | ssh4a 192.0.2.10 deploy --name work --password-stdin
trusted-password-provider | ssh4a profile update work --password-stdin
```

### 登记可信主机指纹

从管理员或可信的服务器控制台获取主机指纹。例如，在目标主机上执行 `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`。将输出中已核对的完整 `SHA256:…` 值赋给 `VERIFIED_FINGERPRINT`，然后在本机登记。

```sh
ssh4a trust set --host 192.0.2.10 --port 22 --fingerprint "$VERIFIED_FINGERPRINT"
ssh4a profile add work --host 192.0.2.10 --user deploy --ask-password
ssh4a exec --profile work -- 'id -un'
```

常规连接遇到未知或变化的主机密钥会中止。确认服务器的密钥变更后，可以用 `trust set` 更新对应主机和端口的记录。

### 跨配置引用

主机、用户名、密码可独立引用。以下示例使用已保存的 `work`、`account` 和 `backup` 三条配置。

```sh
ssh4a exec --profile work --password-ref backup -- 'id -un'
ssh4a exec --host-ref work --user-ref account --password-ref backup --port 2222 -- 'pwd'
```

第一条只用 `backup` 的密码覆盖 `work`，其余连接参数继续来自 `work`。第二条独立选择三个字段，并明确给出非默认端口。需要查看实际值时用 `profile reveal NAME FIELD`，字段可选 `host`、`user`、`port` 和 `password`。显式查询密码会返回明文，普通的 `list` 和 `show` 只显示是否已有密码。

用 `exec` 或 `shell` 发起一次性登录时，可以指定 `--save-as NAME`，在认证成功后创建配置。加 `--save-password` 可同时加密保存本次使用的密码。

## 执行、交互和文件传输

| 需求 | 命令 |
| --- | --- |
| 运行远程命令 | `ssh4a exec --profile work -- 'id -un'` |
| 打开交互式 Shell | `ssh4a shell --profile work --session work-shell` |
| 在本地终端选择配置 | `ssh4a tui` |
| 上传文件 | `ssh4a put --profile work ./build.tar.gz /tmp/build.tar.gz` |
| 下载文件 | `ssh4a get --profile work /tmp/build.tar.gz ./download.tar.gz` |

`exec` 保留远程 stdout、stderr 和退出码。默认可把本地 stdin 转发给远程命令。如果用 `--password-stdin` 提供 SSH 登录密码，远程命令会收到 EOF。用 `--connect-timeout` 和 `--timeout` 分别设置连接和执行时限。执行超时会关闭本地 SSH 通道，远程进程是否已经停止由服务器决定。

Agent 可以通过支持 PTY 的执行工具启动 `shell`，然后向同一进程会话多次写入输入。远端目录、变量和前台程序会延续。输入 `exit` 退出。远程程序正在等待密码时，可从另一个本地命令向指定会话发送密码引用。

```sh
ssh4a session send-secret work-shell --password-ref backup --enter
```

`--enter` 追加回车。远端程序决定自己的输入是否回显。

SFTP 支持单文件。相对远程路径以 SFTP 初始目录为准，相对本地路径以当前工作目录为准。默认保留目标路径上的已有文件。显式使用 `--overwrite` 时，上传需要服务器提供 OpenSSH 的 POSIX rename 扩展。收到 `transfer_unconfirmed` 时，先确认目标文件状态再重试。

## MCP 接入和开机自启

MCP 服务通过 Streamable HTTP 提供工具。默认地址为 `http://127.0.0.1:8744/mcp`，服务以当前系统用户身份运行并使用同一份 SSH 配置。需要 Ubuntu 的 systemd 用户服务。

```sh
ssh4a mcp enable
ssh4a mcp status
```

`status` 显示地址、运行状态和开机启动条件。若 `linger` 为 `false`，可运行 `loginctl enable-linger`，让已启用的服务在用户登录前启动。端口已占用时可用 `ssh4a mcp enable --port 8745`，并在客户端使用对应地址。

在本地终端运行以下命令，给 Codex 签发令牌，并让 Codex 从环境变量读取。

```sh
export SSH4A_MCP_TOKEN="$(ssh4a mcp token create codex)"
codex mcp add ssh4a --url http://127.0.0.1:8744/mcp --bearer-token-env-var SSH4A_MCP_TOKEN
codex
```

后续启动 Codex 时，由本地凭据来源提供同一个 `SSH4A_MCP_TOKEN`。为其他 Agent 单独签发令牌，并在其 Streamable HTTP 客户端配置同一个地址和该 Agent 的 Bearer 令牌。使用代理时，把本机回环地址加入客户端的代理排除项。已签发的令牌名称可以通过 `ssh4a mcp token list` 查看，并可通过 `ssh4a mcp token revoke NAME` 单独撤销。

| 用途 | MCP 工具 |
| --- | --- |
| 查询连接配置 | `profile_list`、`profile_show`、`profile_reveal` |
| 执行远程命令 | `exec` |
| 传输文件 | `file_put`、`file_get` |
| 操作远程 PTY | `shell_open`、`shell_read`、`shell_write`、`shell_resize`、`shell_send_secret`、`shell_close` |

以下是 MCP `exec` 调用的参数示例。

```json
{"connection": {"profile": "work", "password_ref": "backup"}, "command": "id -un"}
```

MCP 文件传输的本地路径以运行服务的主机为准，建议传绝对路径。`shell_open` 返回会话 ID。连续读取时传上次返回的 `cursor`，结果带有截断标记和原始字节的 Base64 表示。

卸载前先停用 MCP 服务。

```sh
ssh4a mcp disable
uv tool uninstall ssh4a
```

## 本地数据和凭据

配置默认保存在 `$XDG_CONFIG_HOME/ssh4a/`，缺省为 `~/.config/ssh4a/`。可在子命令前传 `--config-dir PATH` 选择当前用户的独立目录。目录和文件分别使用 `0700` 和 `0600`。root 可以显式指定其他用户的目录来进行只读查询。

| 文件 | 内容 |
| --- | --- |
| `profiles.json` | 连接参数、已信任主机指纹和 SSH 密码密文 |
| `vault.key` | 解密 SSH 密码所需的本地随机密钥 |
| `mcp/tokens.json` | MCP 令牌摘要和客户端标识 |

MCP 令牌用于连接 ssh4a 服务。远端登录使用本次调用选定的 SSH 凭据。两套凭据可独立轮换。有效 MCP 令牌可调用服务用户的 SSH 工具，包括显式查询字段原值，应单独签发给可信客户端。

同时持有密文和本地密钥的进程具备解密能力。备份时保护整个配置目录，避免提交运行时数据。密钥丢失时可恢复对应备份，或选用新目录重新登记。停用服务和卸载程序会保留用户数据。

## 故障排查

用状态文件区分远程命令退出和本地客户端失败。例如，远程命令和客户端故障都可能返回 `255`，状态文件中的 `kind` 分别为 `remote_exit` 和 `client_error`。

```sh
ssh4a exec --profile work --status-file ./ssh4a-status.json -- 'id -un'
```

| 现象 | 检查内容 |
| --- | --- |
| `host_key` | 确认实际连接的主机和端口，以及已信任的指纹 |
| `authentication` | 核对用户名、选择的密码来源和服务器的密码认证设置 |
| `connection` 或 `timeout` | 用执行失败的同一环境检查目标地址、端口和出站 TCP 权限。若直连返回 `EPERM`，系统在 SSH 握手前拒绝了连接 |
| MCP 返回 HTTP `401` | 确认客户端持有有效令牌，且把它发往正确的 MCP 地址 |

## 开发、构建和验证

```sh
uv sync --locked
uv run ruff check src tests
uv run ruff format --check src tests
uv run pytest
uv build
```

Python 包版本为 `0.0.1a0`，CLI 显示 `0.0.1-alpha`。构建产物在 `dist/`。SSH 集成测试需要 Docker。测试容器仅向本机回环地址发布端口，每次启动都会生成独立的主机密钥。

```sh
docker build -f tests/sshd.Dockerfile -t ssh4a-fixture:local .
SSH4A_TEST_DOCKER=1 uv run pytest
```

现行行为见 [OpenSpec 主规格](openspec/specs/)，首版的设计、任务和增量规格见 [ssh4a-alpha 归档](openspec/changes/archive/2026-11-25-ssh4a-alpha/)。

## 许可证

[MIT](LICENSE)
