import secrets
import sqlite3
import time
from contextlib import contextmanager


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS cursor(mailbox TEXT PRIMARY KEY, validity INTEGER NOT NULL, uid INTEGER NOT NULL,
                                          indexed INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS seen(message_id TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS sessions(code TEXT PRIMARY KEY, codex_id TEXT, directory TEXT NOT NULL, created INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS messages(message_id TEXT PRIMARY KEY, code TEXT NOT NULL REFERENCES sessions(code));
        CREATE TABLE IF NOT EXISTS incoming(
          id INTEGER PRIMARY KEY, validity INTEGER NOT NULL, uid INTEGER NOT NULL,
          message_id TEXT, sender TEXT, subject TEXT, prompt TEXT, reply_to TEXT, refs TEXT,
          code TEXT REFERENCES sessions(code), kind TEXT, state TEXT NOT NULL,
          result TEXT, created INTEGER NOT NULL, process_pid INTEGER,
          UNIQUE(validity,uid), UNIQUE(message_id));
        CREATE TABLE IF NOT EXISTS outgoing(
          id INTEGER PRIMARY KEY, incoming_id INTEGER NOT NULL UNIQUE REFERENCES incoming(id),
          message_id TEXT NOT NULL UNIQUE, recipient TEXT NOT NULL, subject TEXT NOT NULL,
          body TEXT NOT NULL, reply_to TEXT, refs TEXT, state TEXT NOT NULL DEFAULT 'pending',
          attempts INTEGER NOT NULL DEFAULT 0, next_attempt INTEGER NOT NULL DEFAULT 0,
          last_error TEXT);
        """)
        incoming_columns = {row[1] for row in self.db.execute("PRAGMA table_info(incoming)")}
        if "process_pid" not in incoming_columns:
            self.db.execute("ALTER TABLE incoming ADD COLUMN process_pid INTEGER")
        cursor_columns = {row[1] for row in self.db.execute("PRAGMA table_info(cursor)")}
        if "indexed" not in cursor_columns:
            self.db.execute("ALTER TABLE cursor ADD COLUMN indexed INTEGER NOT NULL DEFAULT 0")
        self.db.commit()

    @contextmanager
    def tx(self):
        with self.db:
            yield self.db

    def cursor(self):
        return self.db.execute("SELECT * FROM cursor WHERE mailbox='INBOX'").fetchone()

    def set_cursor(self, validity, uid):
        self.db.execute("INSERT INTO cursor(mailbox,validity,uid,indexed) VALUES ('INBOX',?,?,1) "
                        "ON CONFLICT(mailbox) DO UPDATE SET validity=excluded.validity,uid=excluded.uid,indexed=1",
                        (validity, uid))

    def has_seen(self, message_id):
        return bool(self.db.execute("SELECT 1 FROM seen WHERE message_id=?", (message_id,)).fetchone())

    def mark_seen(self, message_id):
        if message_id:
            self.db.execute("INSERT OR IGNORE INTO seen VALUES (?)", (message_id,))

    def session(self, code):
        return self.db.execute("SELECT * FROM sessions WHERE code=?", (code,)).fetchone()

    def by_message(self, message_id):
        row = self.db.execute("SELECT code FROM messages WHERE message_id=?", (message_id,)).fetchone()
        return row[0] if row else None

    def create_session(self, directory):
        while True:
            code = secrets.token_hex(5).upper()
            if not self.session(code):
                break
        self.db.execute("INSERT INTO sessions VALUES (?,?,?,?)", (code, None, str(directory), int(time.time())))
        return code

    def map_message(self, message_id, code):
        if message_id and code:
            self.db.execute("INSERT OR IGNORE INTO messages VALUES (?,?)", (message_id, code))

    def queue_incoming(self, validity, uid, message_id, sender, subject, prompt, reply_to, refs, code, kind, state):
        cur = self.db.execute("""INSERT OR IGNORE INTO incoming(validity,uid,message_id,sender,subject,prompt,reply_to,refs,code,kind,state,created)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (validity, uid, message_id, sender, subject, prompt, reply_to, refs, code, kind, state, int(time.time())))
        if cur.rowcount:
            return cur.lastrowid
        return None

    def next_task(self):
        return self.db.execute("SELECT * FROM incoming WHERE state='queued' ORDER BY id LIMIT 1").fetchone()

    def interrupted(self):
        return self.db.execute("SELECT * FROM incoming WHERE state='running'").fetchall()

    def next_outgoing(self):
        return self.db.execute("SELECT * FROM outgoing WHERE state='pending' AND next_attempt<=? ORDER BY id LIMIT 1", (int(time.time()),)).fetchone()
