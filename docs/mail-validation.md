# 邮件验证记录

验证日期：2026-09-25。通过 Python 标准库 IMAP 客户端检查真实邮箱；本轮未发送邮件，也未启动 Codex 任务。

## 访问方式

- 收件服务器：`imap.163.com:993`，TLS。
- 账户：一个 163 个人邮箱，具体地址已脱敏。
- 使用只读 `EXAMINE` 和 `BODY.PEEK`，不修改已读状态。
- 首次读取返回 `EXAMINE Unsafe Login`。
- 显式发送包含 `name`、`version`、`vendor`、`support-email` 的 IMAP `ID` 后，返回 `ID completed`，随后成功读取。

只能确认上述完整 ID 参数组合在本次访问中可用，未单独验证每个字段是否必需。

一次 `UID SEARCH FROM "@qq.com"` 没有找到实际存在的 QQ 来信，随后读取近期邮件头成功定位。原因尚未调查；接收实现不应依赖这一搜索结果筛选任务，应按 UID 收取后在本地校验来源。

## 新建样本

| 字段 | 内容 |
| --- | --- |
| 日期 | 2026-09-25 |
| From | `<user@qq.com>` |
| To | `<user@163.com>` |
| Subject | `/new example-project`（示意） |
| 新增正文 | 已脱敏 |
| X-Mailer | `MailMasterAndroid/7.26.1_(16)` |
| MIME | `multipart/alternative`，同时含 `text/plain` 和 `text/html` |
| Message-ID | 存在，QQ 生成 |
| In-Reply-To / References | 均不存在 |

HTML 为正文 div 加空的 `imail_signature` 签名容器。163 的 `Authentication-Results` 报告 SPF 和 DKIM 通过。

## 往返回复样本

用户手动模拟了 QQ 发起 → 163 回复 → QQ 再回复。以下为最后一封 QQ 回复：

| 字段 | 内容 |
| --- | --- |
| 日期 | 2026-09-25 |
| Subject | `回复：回复：/new example-project`（示意） |
| 新增正文 | 已脱敏 |
| In-Reply-To | 指向中间 163 回复的 Message-ID |
| References | 包含最初 QQ 邮件及中间 163 回复的 Message-ID |
| X-Mailer | `MailMasterAndroid/7.26.1_(16)` |

`References` 中 ID 的实际排列形态如下，示意 ID 已替换：

```text
<original@qq.com><reply@163.com>
```

纯文本结构：

```text
本轮新增内容
---- 回复的原邮件 ----
[163 回复的发件人、日期、收件人和主题]
先前的结果
---- 回复的原邮件 ----
[最初 QQ 邮件的发件人、日期、收件人和主题]
最初的输入
```

HTML 的历史内容位于 `div.ntes-mailmaster-quote` 中，包含嵌套的更早引用。去除整个外层引用容器后，可得到本轮新增正文。

## 已得结论及限制

1. 当前客户端的往返回复保留了可用于关联的邮件头。
2. 保存初始邮件映射即可通过本次样本的 `References` 找回原会话；仍应保存服务所有出站邮件映射，以支持直接回复。
3. 不能按标题相同判断同一会话，也不能因回复标题含 `/new` 就新建。
4. MIME 纯文本可直接作为提取基础，HTML 提供结构化引用标记。
5. 这是一个新建样本和一个往返回复样本，不代表不同客户端和所有回复方式均可靠。
6. SMTP 自动发送、身份校验实现、Codex 会话恢复和故障恢复仍未验证。

文档不保存授权码或完整原始邮件；这里只保留实现所需的格式观察。
