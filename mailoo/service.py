import argparse
import fcntl
import imaplib
import json
import logging
import os
import re
import select
import signal
import smtplib
import subprocess
import time
import uuid
from email.message import EmailMessage
from email import policy
from email.parser import BytesParser
from email.utils import formatdate
from pathlib import Path

from .message import CODE, authenticated, ids, new_body, parse, safe_directory, subject_command
from .store import Store

LOG = logging.getLogger("mailoo")


def config(path):
    path = Path(path).expanduser().resolve()
    if path.stat().st_mode & 0o077:
        raise ValueError("配置文件必须为 0600")
    if path.suffix == ".json":
        if path.parent.stat().st_mode & 0o077:
            raise ValueError("JSON 配置目录必须为 0700")
        data = json.loads(path.read_text())
    else:
        values = {}
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition("=")
            if not sep or not key.strip():
                raise ValueError(".env 包含无效行")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key.strip()] = value
        required = ("MAIL_USERNAME", "MAIL_AUTHORIZATION_CODE", "MAIL_ALLOWED_SENDER",
                    "MAIL_RESULT_RECIPIENT", "WORKSPACE_ROOT")
        if any(not values.get(key) for key in required):
            raise ValueError(".env 缺少邮件账户、授权码、允许发件人、结果收件人或工作目录")
        data = {
            "mail": {
                "username": values["MAIL_USERNAME"],
                "authorization_code": values["MAIL_AUTHORIZATION_CODE"],
                "allowed_sender": values["MAIL_ALLOWED_SENDER"],
                "result_recipient": values["MAIL_RESULT_RECIPIENT"],
                "imap_host": values.get("MAIL_IMAP_HOST", "imap.163.com"),
                "imap_port": int(values.get("MAIL_IMAP_PORT", "993")),
                "smtp_host": values.get("MAIL_SMTP_HOST", "smtp.163.com"),
                "smtp_port": int(values.get("MAIL_SMTP_PORT", "465")),
            },
            "codex": {
                "model": values.get("CODEX_MODEL", "gpt-6-luna"),
                "allow_model_fallback": values.get("CODEX_ALLOW_MODEL_FALLBACK", "false").lower() == "true",
            },
            "workspace_root": values["WORKSPACE_ROOT"],
            "task_timeout_seconds": int(values.get("TASK_TIMEOUT_SECONDS", "3600")),
            "poll_interval_seconds": int(values.get("POLL_INTERVAL_SECONDS", "30")),
        }
    if data["codex"]["model"] != "gpt-6-luna" or data["codex"]["allow_model_fallback"] is not False:
        raise ValueError("必须显式使用 gpt-6-luna，且关闭模型回退")
    if data["mail"]["allowed_sender"].lower() != data["mail"]["result_recipient"].lower():
        raise ValueError("允许发件人与固定结果收件人必须相同")
    for address in (data["mail"]["username"], data["mail"]["allowed_sender"],
                    data["mail"]["result_recipient"]):
        if not re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", address):
            raise ValueError("邮件地址格式无效")
    if not data["mail"]["username"].lower().endswith("@163.com") or not data["mail"]["allowed_sender"].lower().endswith("@qq.com"):
        raise ValueError("当前版本要求 163 收件账户和 QQ 指令账户")
    if (data["mail"]["username"].startswith("your-address@") or
            data["mail"]["allowed_sender"].startswith("your-address@") or
            data["mail"]["authorization_code"].startswith("replace-with")):
        raise ValueError("请先替换 .env.example 中的占位内容")
    return data


def imap_open(cfg):
    mail = cfg["mail"]
    imaplib.Commands["ID"] = ("NONAUTH", "AUTH")
    m = imaplib.IMAP4_SSL(mail["imap_host"], mail["imap_port"], timeout=30)
    m._simple_command("ID", f'("name" "mail-oo" "version" "0.1" "vendor" "local" "support-email" "{mail["username"]}")')
    m.login(mail["username"], mail["authorization_code"])
    typ, _ = m.select("INBOX", readonly=True)
    if typ != "OK":
        raise RuntimeError("无法只读打开 INBOX")
    validity = int(m.response("UIDVALIDITY")[1][0])
    return m, validity


def route(store, msg, subject):
    reply_ids = ids(msg.get("In-Reply-To"))
    refs = ids(msg.get("References"))
    reply_codes = {store.by_message(x) for x in reply_ids} - {None}
    ref_codes = {store.by_message(x) for x in refs} - {None}
    token_codes = set(CODE.findall(subject))
    command, arg = subject_command(subject)
    if command == "resume":
        token_codes.add(arg)
    if len(reply_codes | ref_codes | token_codes) > 1:
        raise ValueError("邮件关联指向多个不同会话")
    code = next(iter(reply_codes or ref_codes or token_codes), None)
    if code:
        if not store.session(code):
            raise ValueError("会话编号不存在")
        return code, "resume", None
    if reply_ids or refs or re.match(r"(?i)^(re|回复)\s*[:：]", subject):
        raise ValueError("回复无法关联到已知会话")
    if command == "resume":
        raise ValueError("会话编号不存在")
    if command != "new":
        raise ValueError("新邮件标题必须为 /new 或 /new 相对路径")
    return None, "new", arg


def enqueue_outgoing(store, cfg, incoming_id, code, incoming_mid, refs, body):
    mid = f"<{uuid.uuid4().hex}@{cfg['mail']['username'].split('@')[1]}>"
    subject = f"[mail-oo:{code}] Codex 回复" if code else "[mail-oo] 邮件处理失败"
    references = list(dict.fromkeys(ids(refs) + ids(incoming_mid)))
    store.db.execute("""INSERT INTO outgoing(incoming_id,message_id,recipient,subject,body,reply_to,refs)
        VALUES (?,?,?,?,?,?,?)""", (incoming_id, mid, cfg["mail"]["result_recipient"], subject, body,
                                    incoming_mid, " ".join(references)))
    store.map_message(mid, code)


def ingest(store, cfg, validity, uid, raw):
    try:
        msg, sender = parse(raw)
    except ValueError:
        return
    mail = cfg["mail"]
    if sender != mail["allowed_sender"].lower() or not authenticated(msg, sender, raw):
        LOG.info("ignored UID %s: sender authentication failed", uid)
        return
    if msg.get("Auto-Submitted", "no").lower() != "no" or msg.get("Precedence", "").lower() in ("bulk", "list", "junk"):
        return
    mids = ids(msg.get("Message-ID"))
    if len(mids) != 1:
        LOG.warning("ignored UID %s: invalid Message-ID", uid)
        return
    mid = mids[0]
    if store.db.execute("SELECT 1 FROM incoming WHERE message_id=?", (mid,)).fetchone():
        return
    subject = str(msg.get("Subject", "")).strip()
    reply_to = " ".join(ids(msg.get("In-Reply-To")))
    refs = " ".join(ids(msg.get("References")))
    try:
        code, kind, relative = route(store, msg, subject)
        prompt = new_body(msg)
        if not prompt:
            raise ValueError("正文没有本轮新增内容")
        if kind == "new":
            directory = safe_directory(cfg["workspace_root"], relative)
            code = store.create_session(directory)
        incoming_id = store.queue_incoming(validity, uid, mid, sender, subject, prompt, reply_to, refs, code, kind, "queued")
        store.map_message(mid, code)
        LOG.info("queued UID %s session %s", uid, code)
    except (ValueError, OSError, UnicodeError, LookupError) as e:
        incoming_id = store.queue_incoming(validity, uid, mid, sender, subject, None, reply_to, refs, None, "error", "failed")
        if incoming_id:
            enqueue_outgoing(store, cfg, incoming_id, None, mid, refs, f"邮件未执行：{e}")


def collect(store, cfg):
    m = None
    try:
        m, validity = imap_open(cfg)
        typ, response = m.uid("search", None, "ALL")
        if typ != "OK":
            raise RuntimeError("UID SEARCH 失败")
        uids = [int(x) for x in response[0].split()]
        cursor = store.cursor()
        def message_id(raw):
            header = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
            values = ids(header.get("Message-ID"))
            return values[0] if len(values) == 1 and len(header.get_all("Message-ID", [])) == 1 else None

        def index_old(ids_to_index):
            ids_to_index = list(ids_to_index)
            with store.tx():
                for offset in range(0, len(ids_to_index), 50):
                    batch = ids_to_index[offset:offset + 50]
                    sequence = f"{batch[0]}:{batch[-1]}"
                    status, fetched = m.uid("fetch", sequence, "(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
                    if status != "OK":
                        raise RuntimeError(f"无法建立历史邮件索引，UID {sequence}")
                    received = set()
                    for item in fetched:
                        if not isinstance(item, tuple):
                            continue
                        uid_match = re.search(rb"\bUID\s+(\d+)\b", item[0])
                        if not uid_match or not item[1]:
                            raise RuntimeError(f"历史邮件头格式异常，UID {sequence}")
                        received.add(int(uid_match.group(1)))
                        store.mark_seen(message_id(item[1]))
                    if received != set(batch):
                        raise RuntimeError(f"历史邮件索引不完整，UID {sequence}")

        if cursor is None:
            index_old(uids)
            with store.tx():
                store.set_cursor(validity, max(uids, default=0))
            LOG.info("established inbox baseline at UID %s", max(uids, default=0))
            return
        if not cursor["indexed"]:
            if cursor["validity"] != validity:
                raise RuntimeError("旧游标尚未建立历史邮件索引，且 UIDVALIDITY 已改变；需人工核对")
            index_old(x for x in uids if x <= cursor["uid"])
            with store.tx():
                store.set_cursor(validity, cursor["uid"])
            cursor = store.cursor()
        if cursor["validity"] != validity:
            LOG.warning("UIDVALIDITY changed; rescanning mailbox with Message-ID deduplication")
            with store.tx():
                store.set_cursor(validity, 0)
            start = 0
        else:
            start = cursor["uid"]
        for uid in (x for x in uids if x > start):
            typ, data = m.uid("fetch", str(uid), "(BODY.PEEK[])")
            if typ != "OK":
                raise RuntimeError(f"UID {uid} 收取失败")
            raw = b"".join(x[1] for x in data if isinstance(x, tuple))
            if not raw:
                raise RuntimeError(f"UID {uid} 内容为空")
            with store.tx():
                mid = message_id(raw)
                if not mid or not store.has_seen(mid):
                    ingest(store, cfg, validity, uid, raw)
                store.mark_seen(mid)
                store.set_cursor(validity, uid)
    finally:
        if m:
            try:
                m.logout()
            except Exception:
                pass


def run_codex(store, cfg, task, log_dir):
    session = store.session(task["code"])
    if task["kind"] == "resume" and not session["codex_id"]:
        raise RuntimeError("该会话还没有可恢复的 Codex session ID")
    output_path = log_dir / f"task-{task['id']}.last.txt"
    cmd = ["codex", "exec"]
    if task["kind"] == "resume":
        cmd += ["resume"]
    cmd += ["--model", "gpt-6-luna", "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check", "--json", "--output-last-message", str(output_path)]
    if task["kind"] == "resume":
        cmd += [session["codex_id"], "-"]
    else:
        cmd += ["-"]
    LOG.info("starting Codex task %s, session %s", task["id"], task["code"])
    deadline = time.monotonic() + cfg.get("task_timeout_seconds", 3600)
    trace_path = log_dir / f"task-{task['id']}.jsonl"
    with open(trace_path, "w", encoding="utf-8", opener=lambda p, f: os.open(p, f, 0o600)) as trace:
        process = subprocess.Popen(cmd, cwd=session["directory"], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        buffer = b""

        def consume(line):
            decoded = line.decode("utf-8", errors="replace")
            trace.write(decoded + "\n")
            trace.flush()
            try:
                event = json.loads(decoded)
            except json.JSONDecodeError:
                return
            if event.get("type") == "thread.started":
                sid = event.get("thread_id")
                if sid:
                    with store.tx():
                        store.db.execute("UPDATE sessions SET codex_id=? WHERE code=?", (sid, task["code"]))

        try:
            with store.tx():
                store.db.execute("UPDATE incoming SET process_pid=? WHERE id=?", (process.pid, task["id"]))
            process.stdin.write(task["prompt"].encode())
            process.stdin.close()
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Codex 执行超时")
                ready, _, _ = select.select([process.stdout], [], [], min(1, max(0, deadline - time.monotonic())))
                if ready:
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if chunk:
                        buffer += chunk
                        while b"\n" in buffer:
                            line, buffer = buffer.split(b"\n", 1)
                            consume(line)
                    elif process.poll() is not None:
                        break
                if process.poll() is not None:
                    # Drain the pipe after exit; events may remain buffered.
                    continue
            if buffer:
                consume(buffer)
            if process.returncode:
                raise RuntimeError(f"Codex 退出码 {process.returncode}；详见本地 task-{task['id']}.jsonl")
            if not output_path.exists():
                raise RuntimeError("Codex 没有生成最终答复")
            answer = output_path.read_text().strip()
            if not answer:
                raise RuntimeError("Codex 最终答复为空")
            return answer
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            if process.poll() is None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            raise
        finally:
            if process.poll() is not None:
                with store.tx():
                    store.db.execute("UPDATE incoming SET process_pid=NULL WHERE id=?", (task["id"],))


def execute_one(store, cfg, log_dir):
    task = store.next_task()
    if not task:
        return False
    with store.tx():
        store.db.execute("UPDATE incoming SET state='running' WHERE id=?", (task["id"],))
    try:
        result = run_codex(store, cfg, task, log_dir)
        state = "finished"
    except Exception as e:
        LOG.exception("task %s failed", task["id"])
        result = f"Codex 任务未完成：{e}"
        state = "failed"
    with store.tx():
        store.db.execute("UPDATE incoming SET state=?,result=? WHERE id=?", (state, result, task["id"]))
        enqueue_outgoing(store, cfg, task["id"], task["code"], task["message_id"], task["refs"], result)
    return True


def recover(store, cfg):
    def group_exists(pgid):
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        return True

    for task in store.interrupted():
        pid = task["process_pid"]
        if pid:
            if group_exists(pid):
                try:
                    pgid = os.getpgid(pid)
                except ProcessLookupError:
                    raise RuntimeError(f"上轮 Codex 进程组 {pid} 仍在运行，但主进程已退出；暂停服务")
                command = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                                         capture_output=True, text=True, check=False).stdout
                if pgid != pid or "codex" not in command.lower():
                    raise RuntimeError(f"无法确认上轮 Codex 进程 {pid} 的身份，暂停服务")
                os.killpg(pid, signal.SIGTERM)
                for _ in range(10):
                    if not group_exists(pid):
                        break
                    time.sleep(0.5)
                else:
                    os.killpg(pid, signal.SIGKILL)
                    for _ in range(10):
                        if not group_exists(pid):
                            break
                        time.sleep(0.5)
                    else:
                        raise RuntimeError(f"上轮 Codex 进程组 {pid} 未停止，暂停服务")
                LOG.warning("stopped orphan Codex process group %s", pid)
        with store.tx():
            result = "Codex 执行期间服务中断，本轮未自动重跑。可回复此邮件继续已有会话；如果尚无 session ID，请发新邮件使用 /new。"
            store.db.execute("UPDATE incoming SET state='interrupted',result=?,process_pid=NULL WHERE id=?", (result, task["id"]))
            enqueue_outgoing(store, cfg, task["id"], task["code"], task["message_id"], task["refs"], result)


def send_one(store, cfg):
    row = store.next_outgoing()
    if not row:
        return False
    mail = cfg["mail"]
    msg = EmailMessage()
    msg["From"] = mail["username"]
    msg["To"] = mail["result_recipient"]
    msg["Subject"] = row["subject"]
    msg["Message-ID"] = row["message_id"]
    msg["Date"] = formatdate(localtime=True)
    if row["reply_to"]:
        msg["In-Reply-To"] = row["reply_to"]
    if row["refs"]:
        msg["References"] = row["refs"]
    msg.set_content(row["body"])
    try:
        with smtplib.SMTP_SSL(mail.get("smtp_host", "smtp.163.com"), mail.get("smtp_port", 465), timeout=30) as smtp:
            smtp.login(mail["username"], mail["authorization_code"])
            smtp.send_message(msg)
        with store.tx():
            store.db.execute("UPDATE outgoing SET state='sent',attempts=attempts+1,last_error=NULL WHERE id=?", (row["id"],))
        LOG.info("sent outgoing %s", row["id"])
    except Exception as e:
        delay = min(3600, 30 * (2 ** min(row["attempts"], 7)))
        with store.tx():
            store.db.execute("UPDATE outgoing SET attempts=attempts+1,next_attempt=?,last_error=? WHERE id=?",
                             (int(time.time()) + delay, str(e)[:500], row["id"]))
        LOG.exception("SMTP send failed for outgoing %s", row["id"])
    return True


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["run", "once", "status", "check"])
    parser.add_argument("--config")
    args = parser.parse_args(argv)
    project = Path(__file__).resolve().parent.parent
    config_path = Path(args.config) if args.config else (project / ".env" if (project / ".env").exists() else project / ".local/config.json")
    cfg = config(config_path)
    local = project / ".local"
    local.mkdir(mode=0o700, exist_ok=True)
    if args.command == "check":
        print("configuration valid; model gpt-6-luna")
        return
    log_dir = local / "logs"
    log_dir.mkdir(mode=0o700, exist_ok=True)
    logging.basicConfig(filename=log_dir / "service.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    store = Store(local / "state.sqlite3")
    if args.command == "status":
        for row in store.db.execute("SELECT state,count(*) n FROM incoming GROUP BY state"):
            print("incoming", row["state"], row["n"])
        for row in store.db.execute("SELECT state,count(*) n FROM outgoing GROUP BY state"):
            print("outgoing", row["state"], row["n"])
        return
    lock = open(local / "service.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError("服务已经在运行")
    def stop_requested(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop_requested)
    recover(store, cfg)
    try:
        while True:
            try:
                collect(store, cfg)
            except Exception:
                LOG.exception("IMAP collection failed")
            while execute_one(store, cfg, log_dir):
                pass
            while send_one(store, cfg):
                pass
            if args.command == "once":
                break
            time.sleep(cfg.get("poll_interval_seconds", 30))
    except KeyboardInterrupt:
        LOG.info("service stopped")


if __name__ == "__main__":
    main()
