# Interactive Shell

## Purpose

让人和 Agent 都能连续操作同一个真实远程终端，在需要时用本地保存的密码引用回应远程提示，延续会话里的目录、环境和前台程序状态。

## ADDED Requirements

### Requirement: 持续的远程 PTY

系统 SHALL 在本地终端中提供带 PTY 的远程 Shell，双向传递终端字节、同步窗口尺寸，并在离开时恢复本地终端。会话 SHALL 支持来自 Agent 的多次输入，保持同一个远程 Shell 的状态。

#### Scenario: Agent 分次操作同一远程终端

- **GIVEN** Agent 在一个支持交互输入的本地 PTY 中启动 ssh4a
- **WHEN** Agent 先改变远程目录和环境变量，随后在另一批输入中查询它们
- **THEN** 第二批输入看到同一个远程 Shell 中的目录和环境变量

#### Scenario: 远程全屏程序和前台中断

- **WHEN** 调用方在远端运行需要 PTY 的程序、调整窗口，随后用 Ctrl-C 中断前台进程
- **THEN** 远程程序看到新的窗口尺寸或收到相应的中断，Shell 仍可接收输入

#### Scenario: 远程退出或连接断开

- **WHEN** 远程 Shell 退出或 SSH 连接断开
- **THEN** ssh4a 结束本地等待，恢复本地终端，并返回会话结果

### Requirement: 显式向交互会话发送密码引用

系统 SHALL 允许本地调用方为一个已命名的交互会话显式发送任一配置的密码。调用方 SHALL 能控制是否追加 Enter。发送命令和本地状态输出 SHALL 仅使用会话标识和引用。

#### Scenario: 用 B 的密码回应 A 中的远程提示

- **GIVEN** 名为 work-a 的会话已连接 A，远程程序正在等候密码
- **WHEN** Agent 指定会话 work-a、配置 B 的密码引用，并要求追加 Enter
- **THEN** 系统将 B 的实际密码和一个回车发送给该会话
- **AND** 本地控制命令的参数和状态输出不含密码原值

#### Scenario: 控制请求缺少活动会话

- **WHEN** 调用方指定不存在或已退出的会话
- **THEN** 系统报告会话不可用，无需回显密码

#### Scenario: 不同本地帐号无法控制会话

- **WHEN** 另一个普通本地账户尝试向会话发送控制请求
- **THEN** 请求被拒绝

### Requirement: 终端配置选择

系统 SHALL 提供适合人在终端使用的配置选择界面。选定的配置 SHALL 进入同一交互 Shell 流程。

#### Scenario: 从选择界面连接

- **WHEN** 用户在终端选择已保存的配置 A
- **THEN** 系统按与 CLI 相同的字段解析及认证规则进入 A 的远程 Shell
