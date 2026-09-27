import tempfile
import unittest
import dns.exception
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from mailoo.message import dkim_dns, ids, new_body, safe_directory
from mailoo.service import collect, config, enqueue_outgoing, recover, route, send_one
from mailoo.store import Store


class MailooTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "db.sqlite3")
        self.addCleanup(self.store.db.close)
        self.cfg = {"mail": {"username": "owner@163.com", "authorization_code": "unused",
                             "result_recipient": "owner@qq.com"}}

    def test_private_env_configuration(self):
        env = self.root / ".env"
        env.write_text("MAIL_USERNAME=owner@163.com\nMAIL_AUTHORIZATION_CODE=local-test-code\n"
                       "MAIL_ALLOWED_SENDER=owner@qq.com\nMAIL_RESULT_RECIPIENT=owner@qq.com\n"
                       f"WORKSPACE_ROOT={self.root}\n")
        env.chmod(0o600)
        loaded = config(env)
        self.assertEqual(loaded["codex"]["model"], "gpt-6-luna")
        self.assertEqual(loaded["mail"]["result_recipient"], "owner@qq.com")
        env.chmod(0o644)
        with self.assertRaises(ValueError):
            config(env)

    def test_quote_and_adjacent_references(self):
        msg = EmailMessage()
        msg.set_content("new instruction\n---- 回复的原邮件 ----\n发件人: old 日期: yesterday\nold instruction")
        self.assertEqual(new_body(msg), "new instruction")
        self.assertEqual(ids("<one@qq.com><two@163.com>"), ["<one@qq.com>", "<two@163.com>"])
        msg.set_content("The phrase ---- 回复的原邮件 ---- is part of my prompt.")
        self.assertIn("---- 回复的原邮件 ----", new_body(msg))

    def test_html_fallback_removes_quote_and_signature(self):
        msg = EmailMessage()
        msg.set_content('<div>new instruction</div><div class="ntes-mailmaster-quote">old instruction</div><div id="imail_signature">signature</div>', subtype="html")
        self.assertEqual(new_body(msg), "new instruction")

    def test_dkim_dns_timeout_is_retryable(self):
        with patch("mailoo.message.dns.resolver.resolve", side_effect=dns.exception.Timeout):
            with self.assertRaises(RuntimeError):
                dkim_dns(b"selector._domainkey.qq.com")

    def test_paths_reject_escape_and_symlink(self):
        root = self.root / "Documents"
        root.mkdir()
        self.assertEqual(safe_directory(root, "with spaces"), (root / "with spaces").resolve())
        (root / "outside").symlink_to(self.root)
        for name in ("../elsewhere", "/tmp", "outside/child"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                safe_directory(root, name)

    def test_reply_subject_new_does_not_start_session(self):
        with self.store.tx():
            code = self.store.create_session(self.root)
            self.store.map_message("<first@qq.com>", code)
        msg = EmailMessage()
        msg["References"] = "<first@qq.com><middle@163.com>"
        msg["In-Reply-To"] = "<middle@163.com>"
        self.assertEqual(route(self.store, msg, "回复：/new testfolder")[:2], (code, "resume"))
        msg.replace_header("References", "<unknown@qq.com>")
        with self.assertRaises(ValueError):
            route(self.store, msg, "回复：/new testfolder")

    def test_conflicting_references_are_rejected(self):
        with self.store.tx():
            first = self.store.create_session(self.root)
            second = self.store.create_session(self.root)
            self.store.map_message("<one@qq.com>", first)
            self.store.map_message("<two@qq.com>", second)
        msg = EmailMessage()
        msg["References"] = "<one@qq.com><two@qq.com>"
        with self.assertRaises(ValueError):
            route(self.store, msg, "reply")

    def test_restart_marks_running_without_rerun(self):
        with self.store.tx():
            code = self.store.create_session(self.root)
            task = self.store.queue_incoming(1, 1, "<one@qq.com>", "owner@qq.com", "/new", "prompt",
                                             "", "", code, "new", "running")
        recover(self.store, self.cfg)
        self.assertIsNone(self.store.next_task())
        self.assertEqual(self.store.next_outgoing()["incoming_id"], task)
        recover(self.store, self.cfg)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM outgoing").fetchone()[0], 1)

    def test_restart_pauses_for_unverified_orphan_group(self):
        with self.store.tx():
            code = self.store.create_session(self.root)
            task = self.store.queue_incoming(1, 1, "<one@qq.com>", "owner@qq.com", "/new", "prompt",
                                             "", "", code, "new", "running")
            self.store.db.execute("UPDATE incoming SET process_pid=12345 WHERE id=?", (task,))
        with patch("mailoo.service.os.killpg"), patch("mailoo.service.os.getpgid", side_effect=ProcessLookupError):
            with self.assertRaises(RuntimeError):
                recover(self.store, self.cfg)
        self.assertEqual(self.store.db.execute("SELECT state FROM incoming").fetchone()[0], "running")

    def test_smtp_retry_keeps_message_id(self):
        with self.store.tx():
            code = self.store.create_session(self.root)
            task = self.store.queue_incoming(1, 1, "<one@qq.com>", "owner@qq.com", "/new", "prompt",
                                             "", "", code, "new", "finished")
            enqueue_outgoing(self.store, self.cfg, task, code, "<one@qq.com>", "", "answer")
        mid = self.store.next_outgoing()["message_id"]
        with patch("mailoo.service.smtplib.SMTP_SSL", side_effect=OSError("offline")), patch("mailoo.service.LOG.exception"):
            send_one(self.store, self.cfg)
        row = self.store.db.execute("SELECT * FROM outgoing").fetchone()
        self.assertEqual(row["state"], "pending")
        self.assertEqual(row["message_id"], mid)
        with self.store.tx():
            self.store.db.execute("UPDATE outgoing SET next_attempt=0")
        class FakeSMTP:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def login(self, *args): pass
            def send_message(self, msg): self.message = msg
        fake = FakeSMTP()
        with patch("mailoo.service.smtplib.SMTP_SSL", return_value=fake):
            send_one(self.store, self.cfg)
        self.assertEqual(fake.message["Message-ID"], mid)
        self.assertEqual(fake.message["To"], "owner@qq.com")
        self.assertEqual(fake.message["In-Reply-To"], "<one@qq.com>")
        self.assertEqual(fake.message["References"], "<one@qq.com>")
        self.assertIn(code, fake.message["Subject"])
        self.assertEqual(self.store.db.execute("SELECT state FROM outgoing").fetchone()[0], "sent")

    def test_uidvalidity_rescan_skips_baseline_messages(self):
        class FakeIMAP:
            def __init__(self, uids): self.uids = uids
            def uid(self, command, *args):
                if command == "search":
                    return "OK", [b" ".join(str(uid).encode() for uid in self.uids)]
                if ":" in args[0]:
                    low, high = map(int, args[0].split(":"))
                    return "OK", [(f"1 (UID {uid} BODY[HEADER.FIELDS (MESSAGE-ID)]".encode(),
                                   f"Message-ID: <m{uid}@qq.com>\r\n\r\n".encode())
                                  for uid in self.uids if low <= uid <= high]
                uid = int(args[0])
                return "OK", [(b"", f"Message-ID: <m{uid}@qq.com>\r\n\r\nbody".encode())]
            def logout(self): pass
        with patch("mailoo.service.imap_open", return_value=(FakeIMAP([1, 2]), 1)):
            collect(self.store, self.cfg)
        self.assertEqual(self.store.cursor()["uid"], 2)
        self.assertTrue(self.store.has_seen("<m1@qq.com>"))
        processed = []
        with patch("mailoo.service.imap_open", return_value=(FakeIMAP([1, 2, 3]), 2)), \
             patch("mailoo.service.ingest", side_effect=lambda _s, _c, _v, uid, _raw: processed.append(uid)):
            collect(self.store, self.cfg)
        self.assertEqual(processed, [3])
        self.assertEqual(self.store.cursor()["validity"], 2)


if __name__ == "__main__":
    unittest.main()
