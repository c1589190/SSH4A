# MCP Service

## Purpose

让多个同机 Agent 通过一个开机自启的用户级 MCP 服务使用 ssh4a。Agent 接入凭据与远程服务器的 SSH 配置分别管理，连接之后按调用选择目标。

## ADDED Requirements

### Requirement: 用户级 MCP 服务开机启动

系统 SHALL 提供可启用和停用的用户级 MCP 服务。在具备用户开机启动条件的 Ubuntu 上，已启用服务 SHALL 在用户登录前启动，并使用所属用户的 ssh4a 配置。首版 SHALL 通过本机回环地址提供 Streamable HTTP 入口。

#### Scenario: 用户未登录时服务可用

- **GIVEN** 当前账号已启用用户 linger 和 ssh4a MCP 服务
- **WHEN** 测试系统完成开机且该账号尚未登录
- **THEN** 本机认证客户端可以连接服务并发现工具

#### Scenario: 新客户端使用本机地址接入

- **WHEN** 用户启用服务并查看状态
- **THEN** 系统显示服务运行状态和供客户端配置的 MCP URL
- **AND** 状态输出不包含任何令牌原值或 SSH 密码

### Requirement: 独立管理 Agent 接入令牌和 SSH 配置

系统 SHALL 独立存储和管理 MCP 接入令牌和 SSH 配置。服务端存储令牌的校验信息，SSH 配置继续保存主机、用户名和加密密码。用户 SHALL 能为不同 Agent 签发及撤销令牌。有效令牌用于 MCP 鉴权，调用方随后通过调用参数选择 SSH 目标和 SSH 凭据来源。

#### Scenario: Agent 使用同一令牌连接不同服务器

- **GIVEN** Agent 持有已签发的 MCP 令牌，配置中有 A 和 B 两个 SSH 目标
- **WHEN** Agent 使用同一令牌发起两个调用，分别选择 A 和 B
- **THEN** ssh4a 通过同一 MCP 客户端身份接收两个调用，并按各调用提供的 SSH 参数分别验证目标和认证

#### Scenario: 轮换 SSH 密码后继续使用同一 MCP 令牌

- **WHEN** 用户轮换一个服务器的 SSH 密码并更新保存的 SSH 配置
- **THEN** Agent 使用原 MCP 令牌即可再次调用，并使用更新后的 SSH 密码连接

#### Scenario: 撤销单个 Agent 的令牌

- **WHEN** 用户撤销 Agent A 的 MCP 令牌
- **THEN** 使用该令牌的新请求被拒绝，Agent B 的令牌和保存的 SSH 配置仍然有效

#### Scenario: 无有效令牌时拒绝请求

- **WHEN** 客户端没有发送令牌或发送无效令牌
- **THEN** 服务在工具发现与调用前拒绝请求

### Requirement: 暴露共享 SSH 工具

系统 SHALL 为已认证的 MCP 客户端提供连接配置查询、指定字段原值查询、无头远程执行、单文件上传下载和交互式 Shell 工具。SSH 操作 SHALL 使用共享的配置解析和主机身份校验。常规工具结果 SHALL 隐藏密码，显式字段原值查询 SHALL 支持返回真实密码。

#### Scenario: 有效客户端以引用运行命令和传文件

- **WHEN** 客户端指定 A 的主机和用户名及 B 的密码引用，调用远程命令和文件传输
- **THEN** 操作按字段引用完成，并返回各自结果
- **AND** 文件传输的本地路径以运行 ssh4a 服务的主机为准

#### Scenario: 主动查询 SSH 密码原值

- **WHEN** 客户端显式请求一个配置的密码原值
- **THEN** 服务返回解密后的值

### Requirement: 交互式会话按客户端身份隔离

系统 SHALL 允许已认证客户端创建一个真正的远程 PTY，随后按会话标识读取输出、写入按键、调整尺寸、发送密码引用和关闭。同一服务 SHALL 支持多个活动客户端。会话标识 SHALL 绑定到签发的 MCP 客户端身份。

#### Scenario: 多个 Agent 同时操作独立会话

- **GIVEN** Agent A 和 B 分别用自己的令牌打开交互 Shell
- **WHEN** 两个 Agent 交替向各自会话写入命令并读取后续输出
- **THEN** 每个会话保持自己的目录、环境与输出
- **AND** A 使用 B 的会话标识读写时，服务拒绝该调用

#### Scenario: 通过引用回应远程提示

- **WHEN** 客户端显式向自己的远程 PTY 发送一个密码引用
- **THEN** ssh4a 向指定会话写入解析后的密码，并按调用选择追加回车
- **AND** 发送结果不包含密码明文
