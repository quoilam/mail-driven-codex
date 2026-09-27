# mail-driven-codex

在 macOS 上运行的单用户邮件入口：从 163 邮箱收取固定 QQ 地址的命令，用 `gpt-6-luna` 串行运行 Codex，并把每轮最终答复发回该 QQ 地址。设计见 [docs/design.md](docs/design.md)，验证记录见 [docs/verification.md](docs/verification.md)。

## 配置

需要已登录的 Codex CLI，以及启用 IMAP/SMTP 的 163 邮箱授权码。

```sh
python3 -m venv .local/venv
.local/venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
```

在 `.env` 中填写账户、授权码和固定 QQ 地址。`MAIL_ALLOWED_SENDER` 与 `MAIL_RESULT_RECIPIENT` 必须相同。服务还支持旧版 `.local/config.json`，便于已有本机部署继续运行；若两者同时存在，默认使用 `.env`。`.env` 与 `.local/` 都由 `.gitignore` 排除。不要把授权码写入源码、文档或邮件正文。

```sh
.local/venv/bin/python -m mailoo check
.local/venv/bin/python -m mailoo status
.local/venv/bin/python -m mailoo once
.local/venv/bin/python -m mailoo run
```

`once` 与 `run` 都会实际执行待处理任务并发送结果。首次运行索引历史邮件的 Message-ID，建立 UID 基线，不执行历史邮件。之后按持久化游标补收。读取使用只读 `EXAMINE` 和 `BODY.PEEK`，不改变已读状态。服务仅接受指定 QQ 地址，且要求 163 收信认证结果与 QQ DKIM 签名通过。

## 邮件命令

新建邮件的标题为 `/new` 或 `/new path`，正文全部作为 prompt。`path` 相对于 `WORKSPACE_ROOT`，可包含空格，但不能是绝对路径、`~` 路径或包含 `.`、`..` 段。不存在的子目录会创建。附件不参与输入或输出。

收到结果邮件后，直接回复它，在正文顶部写本轮新增内容即可。结果标题包含十位会话编号；也可以用标题 `/resume ABCDEF1234` 手动恢复。命令不区分大小写。不同线程始终串行执行。

## 登录启动

```sh
python3 scripts/launchagent.py install
python3 scripts/launchagent.py status
python3 scripts/launchagent.py uninstall
```

安装脚本根据当前项目位置、Python 虚拟环境和 Codex CLI 路径生成用户 LaunchAgent，安装后立即启动，并在登录后自动运行。退出登录或服务崩溃中断的任务不会自动重跑；重新启动会先确认上一轮 Codex 已停止。Mac 唤醒后下一次轮询会补收邮件。

## 运维

`.local/venv/bin/python -m mailoo status` 显示任务和发件状态计数。日志在 `.local/logs/service.log`，Codex 事件与最终答复在 `task-N.jsonl` 和 `task-N.last.txt`，SQLite 状态在 `.local/state.sqlite3`。备份前停止服务，保留 `.env` 与 `.local/` 的私有权限。更新授权码后重启服务。

SMTP 对端已接受但本地尚未记录成功时，重试可能造成重复邮件；重试会复用相同 Message-ID，且不会重新执行 Codex。
