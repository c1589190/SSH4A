# Design

## Context

这是 ssh4a 的首个版本。工作区已有独立的 OpenSpec 根目录，运行基线是本机 Ubuntu 24.04 和 Python 3.12。当前 Codex 执行环境已用本地 Bash 验证 PTY 可以持续接收输入。本机的 systemd 用户已启用 linger，已安装的 Codex CLI 支持配置 Streamable HTTP 及 Bearer 令牌。 [systemd 说明](https://www.freedesktop.org/software/systemd/man/latest/loginctl.html) · [Codex MCP 配置](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)

## Goals / Non-Goals

设计以用户级配置和有权访问该配置的本地调用方为边界。首版连接基线是直接连到启用标准 SSH 密码认证的服务器。终端界面覆盖连接选择，远程 Shell 自身提供完整终端交互。交付包括可在本机独立使用的命令行包，以及启用后可开机启动、供同机客户端访问的 MCP 服务。

## Decisions

### 进程与模块

使用 Python 3.12、标准库 `argparse`、Paramiko、cryptography 和官方 MCP Python SDK。由 `cli` 和 `mcp` 适配各自的输入与输出，共用配置、凭据、SSH、Shell 和文件传输实现。TTY 菜单调用与本地 `shell` 相同的入口。每次 CLI 调用开启所需 SSH 连接，交互调用在会话期间保留该进程。MCP 服务在其常驻进程内维护自身的 SSH 会话。Paramiko 在应用进程内接收密码、打开远程 PTY 和 SFTP，便于共享解析、校验和错误处理。 [Paramiko Transport](https://docs.paramiko.org/en/stable/api/transport.html) · [MCP Python SDK](https://py.sdk.modelcontextprotocol.io/run/deploy/)

```text
CLI / TUI -----------------------------+
                                       |
Agent -> MCP HTTP -> verify MCP token -+
                                       |
                                  Shared SSH operations
                                       |
                        Resolve SSH fields from user config
                                       |
                        Verify target SSH host key
                                       |
                        Authenticate to target SSH server
                                       |
                                exec / SFTP / PTY
```

### 用户目录和密码加密

- 默认目录为 `$XDG_CONFIG_HOME/ssh4a/`，回退到 `~/.config/ssh4a/`。这里将“当前目录配置”理解为当前用户的 ssh4a 配置目录。显式 `--config-dir PATH` 可选择当前用户拥有的其他目录，便于测试和隔离多个配置集合。目录默认权限 `0700`，文件默认权限 `0600`。检查目录和文件的归属、类型与权限。root 平时使用自己的目录，可显式选中另一用户的目录进行只读查询和解密。 [XDG Base Directory Specification](https://specifications.freedesktop.org/basedir/latest/)
- `profiles.json` 保存版本、配置记录和主机指纹清单。记录含别名、主机、端口、用户名和可选密码密文。手工可读的 JSON 适合首版少量记录。信息更新使用同一把文件锁、同目录临时文件和原子替换，避免两个 CLI 并发写时覆盖彼此的更改。字段解析读取一个一致的配置快照。
- `vault.key` 是同一用户目录内的独立随机密钥文件，首次保存密码时创建。使用库的 AES-256-GCM，每次写入使用新随机 nonce，将格式版本、记录 ID 和密码字段名作为关联数据。配置更新和密钥创建在同一配置锁下处理。已有密文时，密钥缺失、文件权限不符或认证标签不匹配均返回错误，保留原数据。与需要登录解锁的系统凭据库相比，本地密钥满足当前 Ubuntu 的重启后无头需求。 [cryptography AESGCM](https://cryptography.io/en/stable/hazmat/primitives/aead/#cryptography.hazmat.primitives.ciphers.aead.AESGCM)
- 主机、用户名和端口可正常查看。密码通过无回显终端或 `--password-stdin` 接收。无头标准输入读到 EOF，只移除一个约定的末尾 LF 或 CRLF，保留其他空白。密码正常流经内存与 SSH，不写入命令参数或应用日志。`profile reveal NAME password` 是显式输出明文的入口，无需 TTY 或额外确认。

### 字段解析和一次性连接

共享连接入口接收可选的 `--profile NAME` 作为基础。对每个字段使用明确提供的字面值、引用或密码输入源覆盖基础值。单个字段只能选择一个显式来源，未覆盖的字段使用基础值，端口缺省为 22。`--host-ref`、`--user-ref` 和 `--password-ref` 各按给定名称读取相应字段。引用只在调用时解析，最终保存的是实际值。连接 A 可明确指定 B 的密码，也可只替换主机或用户名并保留所选基础配置的其他字段。

命令示例使用虚构的配置别名和保留 IP。

```sh
ssh4a profile show a
ssh4a profile reveal b password
ssh4a shell --profile a --password-ref b --session work-a
ssh4a shell --host-ref a --user-ref deploy-account --password-ref b
ssh4a shell --host 192.0.2.10 --user deploy --ask-password --save-as a --save-password
ssh4a session send-secret work-a --password-ref b --enter
```

`profile add`、`profile update` 支持外部程序经标准输入设置密码。`exec --password-stdin` 将标准输入用于本次认证，远程命令获得 EOF。`shell` 使用终端传递键盘字节，一次性密码通过 `--ask-password` 在本地无回显录入，已有配置可直接按引用连接。`shell` 和 `exec` 在主机验证及认证成功后处理显式 `--save-as`，新增别名时可用 `--save-password` 选择将实际认证密码一并加密保存。同名配置通过 `profile update` 修改。

### SSH 主机信任和无头执行

先建 TCP 和 Paramiko Transport，通过密钥交换获得主机公钥，根据实际目标的主机名或 IP 和端口比对已登记的 SHA-256 指纹，再进行密码认证。由 `trust set --host HOST --port PORT --fingerprint SHA256:…` 显式登记或更换已独立验证的指纹。常规连接遇到未知或变化的密钥会以稳定的错误类型结束。按实际目标检查指纹可以支持独立组合字段，密码来源的主机无需参与。 [Paramiko Transport](https://docs.paramiko.org/en/stable/api/transport.html) · [OpenSSH 主机密钥说明](https://man.openbsd.org/ssh)

`ssh4a IP[:PORT] USER` 与 `ssh4a add HOST[:PORT] USER` 进入同一个一键添加流程。IPv6 需要内联端口时使用 `[IPv6]:PORT`。可单独传 `--port`，两种写法的端口冲突时在读取密码前报错。默认以无回显终端提示接收密码，也可显式选择 `--password-stdin`。输入有效时立即发起 SSH 密码认证。未知主机在这条路径上接受同一次握手返回的密钥，已有信任记录照常校验。只有认证成功后，才在配置锁下复核信任记录并用一次原子写入保存主机指纹和包含密码密文的配置。并发写入不同指纹时停止添加，以保留现有信任。别名默认由用户名、主机和非默认端口生成，调用方可用 `--name` 指定。

`exec` 默认无 PTY，接收一个交给远端 Shell 的命令字符串，不在本地再执行一次 Shell 解析。持续并发排空远程 stdout 和 stderr，转发可选 stdin 及 EOF，完成后取远程退出码。连接和执行各有时限。CLI 故障使用退出码 255 和带类型的 stderr 诊断。需要无歧义区分时，`--status-file PATH` 将 JSON 状态原子写入调用方选定的本地路径。状态使用 `kind=remote_exit` 及远程退出码，或 `kind=client_error` 及错误类型。 [Paramiko Channel](https://docs.paramiko.org/en/stable/api/channel.html)

### 交互式 PTY 和活动会话

- `shell` 在 SSH 端申请与本地终端匹配的 PTY 并开启 Shell，本地使用原始终端模式双向转发。按键、ANSI 控制序列和 Ctrl-C 作为字节传输，SIGWINCH 更新远端尺寸，退出或异常时关闭 SSH 通道并恢复本地终端。Agent 在一个持续的本地 PTY 中启动进程，然后向同一个本地 PTY 写入后续命令。TUI 选择器在交接给 Shell 前退出自己的显示状态。 [Paramiko Channel PTY](https://docs.paramiko.org/en/stable/api/channel.html#paramiko.channel.Channel.get_pty)
- `--session ID` 显式启用本地会话控制。进程在用户私有的运行目录中创建 Unix 域套接字，运行目录优先使用 `$XDG_RUNTIME_DIR`，否则使用选定配置目录下的私有运行子目录。套接字按本地 UID 和配置目录区分，通过文件权限及 `SO_PEERCRED` 验证同 UID。重复会话名明确失败，正常退出时回收套接字。路径超出系统长度限制时，明确报告并支持使用较短的用户运行路径。这个控制入口由交互会话进程自身提供。 [Linux Unix domain sockets](https://man7.org/linux/man-pages/man7/unix.7.html)
- `session send-secret` 向活动进程发送会话标识对应的请求，载荷仅包含密码来源别名和是否追加 Enter。会话进程在自己的配置范围内读取并解密密码，然后由终端转发循环按顺序写到同一个远程 PTY，避免与普通本地输入交错。返回值只确认输入成功写入。相比解释普通终端文本中的占位符，显式控制命令允许远程程序自由使用终端输入，也为本地调用方提供独立的成功或失败结果。

### 单文件 SFTP

使用同一连接解析、信任检查和认证流程。只向目标同目录临时文件写入，关闭并确认传输完成后发布。默认上传用标准 SFTP rename，下载用本地不替换发布操作；`--overwrite` 分别使用 OpenSSH 的 POSIX rename 扩展和本地原子替换。远端缺少所需扩展时保留原文件并报告失败。 [Paramiko SFTP](https://docs.paramiko.org/en/stable/api/sftp.html)

### MCP 接入凭据与 SSH 凭据

MCP 的 Bearer 令牌由用户为 Agent 单独签发，用于鉴权进入 ssh4a 的 MCP 端点。服务端在同一用户配置目录下的 `mcp/tokens.json` 保存客户端标识和高熵随机令牌的摘要，原值在签发时交给客户端保存。请求在工具发现和调用前通过令牌校验。服务使用最新的令牌记录判断后续请求，并按客户端身份隔离活动会话。一个令牌对应一个客户端身份；该身份可以在每次调用时选择不同的 SSH 配置和字段引用。

SSH 资料继续保存在 `profiles.json`，SSH 密码通过 `vault.key` 解密。MCP 工具使用引用时，由共享 SSH 实现验证实际目标的主机指纹，再用解析出的 SSH 凭据认证。SSH 密码轮换和 MCP 令牌签发、撤销分别从各自的 CLI 入口完成。显式查询密码的 MCP 工具返回真实值，其明文可能出现在调用方记录中。服务日志隐藏令牌、SSH 密码和原值查询结果。

### MCP 服务与首次接入

官方 Python SDK 提供 Streamable HTTP。默认地址为 `http://127.0.0.1:8744/mcp`，端口可以在启用服务时指定。同时校验 HTTP Host 和 Origin，使用独立的客户端令牌。以 `systemd --user` 的 `ssh4a-mcp.service` 启动，unit 指向已安装命令的稳定绝对路径和选定的用户配置目录，随默认目标启动并在进程异常退出时重启。用户 linger 使已启用的服务在用户登录前可运行。当前首版验收范围是可以访问该本机回环地址的 Agent。 [MCP Python SDK 部署文档](https://py.sdk.modelcontextprotocol.io/run/deploy/) · [systemd 用户 linger](https://www.freedesktop.org/software/systemd/man/latest/loginctl.html)

首次接入按以下流程验收。

1. 运行 `ssh4a mcp enable` 启动并启用用户服务。命令显示 MCP 地址，端口冲突或启动失败时返回可定位的原因。`ssh4a mcp status` 查看服务状态、URL 和开机启动条件。
2. 在自己的终端用 `ssh4a mcp token create codex` 给 Codex 签发独立令牌。命令只在签发时显示令牌原值。将 MCP 地址填入 Codex 的服务配置，令牌可由 `bearer_token_env_var` 从本地环境读取。 [Codex MCP 配置](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
3. Agent 发现工具后，通过配置列表和配置别名选择目标。再为第二个 Agent 签发独立令牌时，可沿用相同 MCP 地址和现有 SSH 配置。

MCP 无头执行返回结构化 stdout、stderr、远程退出状态或客户端故障。SFTP 参数中的本地文件路径由服务进程解析。交互式 Shell 工具提供打开、读取、写入、调整尺寸、发送密码引用和关闭。创建时返回应用层会话 ID，后续请求同时检查该 ID 和令牌所属身份。服务为每个 PTY 保留有限输出，按游标读取并报告截断，使用同一写入顺序发送按键和密码。空闲超时、关闭或令牌撤销时清理所属会话。一次性 SSH 连接随调用结束，交互连接随会话结束。长时间 I/O 交给有界后台执行，避免阻塞其他客户端工具调用。

## Risks / Trade-offs

- **密钥和密文可被同时读取。** 仅泄露 `profiles.json` 时密码仍为密文。持有目录和密钥的 root 或本地同 UID 程序具备解密能力，目录备份应按凭据保护。密钥丢失时从备份恢复对应密钥。密钥无法恢复时保留旧目录，在新目录中重新录入配置和密码。
- **MCP 令牌授权访问服务用户的 SSH 工具。** 持有有效令牌的客户端可以调用 SSH 工具和显式密码原值查询。给每个可信 Agent 单独签发令牌，便于只撤销受影响的接入。
- **主动查询和远端回显会产生明文。** `profile reveal` 的标准输出可能进入 Agent 工具记录。向远程提示发送引用时，先确认当前提示的用途，远程程序或终端回显可以展示其收到的密码。
- **命令超时和网络断开存在远端状态不确定性。** 本地超时后远端进程可能仍在运行。上传发布请求发出后如果连接立即丢失，报告发布状态无法确认，由调用方检查目标文件。

## Migration Plan

这是新项目。生成 `0.0.1a0` 本地安装包，CLI 显示产品版本 `0.0.1-alpha`。用独立临时配置目录和隔离 OpenSSH 实例完成端到端验收后，将包安装到当前用户的工具环境，并按用户选择启用 MCP 服务。停用服务、卸载程序包与清理配置分别处理。恢复时可独立保留 SSH 记录和撤销 Agent 接入。 [Python 预发布版本规范](https://packaging.python.org/en/latest/specifications/version-specifiers/#pre-releases)
