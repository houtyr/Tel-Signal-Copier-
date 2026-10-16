"""Local SQLite log: Telegram messages, copier events, trade actions, and equity snapshots."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, ts REAL, chat_id INTEGER, chat_title TEXT,
    msg_id INTEGER, reply_to INTEGER, text TEXT, kind TEXT, parsed TEXT, outcomes TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts REAL, level TEXT, account TEXT, text TEXT, message_id INTEGER);
CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY, ts REAL, account TEXT, signal_key TEXT, action TEXT,
    symbol TEXT, side TEXT, volume REAL, ticket INTEGER, detail TEXT);
CREATE TABLE IF NOT EXISTS equity(ts REAL, account TEXT, balance REAL, equity REAL);
CREATE INDEX IF NOT EXISTS eq_idx ON equity(account, ts);
"""


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def _rows(self, sql, *args):
        return [dict(r) for r in self.db.execute(sql, args)]

    # messages
    def add_message(self, chat_id, chat_title, msg_id, reply_to, text, kind, parsed) -> int:
        cur = self.db.execute("INSERT INTO messages(ts,chat_id,chat_title,msg_id,reply_to,text,kind,parsed,outcomes,status)"
                              " VALUES(?,?,?,?,?,?,?,?,?,?)",
                              (time.time(), chat_id, chat_title, msg_id, reply_to, text, kind,
                               json.dumps(parsed) if parsed else None, "[]", "processing" if kind != "none" else "ignored"))
        self.db.commit()
        return cur.lastrowid

    def finish_message(self, mid: int, outcomes: list, status: str):
        self.db.execute("UPDATE messages SET outcomes=?, status=? WHERE id=?", (json.dumps(outcomes), status, mid))
        self.db.commit()

    def update_message(self, mid: int, kind: str | None = None, parsed=None, status: str | None = None):
        if kind is not None:
            self.db.execute("UPDATE messages SET kind=? WHERE id=?", (kind, mid))
        if parsed is not None:
            self.db.execute("UPDATE messages SET parsed=? WHERE id=?", (json.dumps(parsed), mid))
        if status is not None:
            self.db.execute("UPDATE messages SET status=? WHERE id=?", (status, mid))
        self.db.commit()

    def message(self, mid: int) -> dict:
        r = self._rows("SELECT * FROM messages WHERE id=?", mid)
        return self._msg(r[0]) if r else {}

    def messages(self, limit=100) -> list[dict]:
        return [self._msg(r) for r in self._rows("SELECT * FROM messages ORDER BY id DESC LIMIT ?", limit)]

    @staticmethod
    def _msg(r):
        r["parsed"] = json.loads(r["parsed"]) if r["parsed"] else None
        r["outcomes"] = json.loads(r["outcomes"] or "[]")
        return r

    # events / trades
    def add_event(self, level, account, text, message_id=None) -> dict:
        ts = time.time()
        cur = self.db.execute("INSERT INTO events(ts,level,account,text,message_id) VALUES(?,?,?,?,?)",
                              (ts, level, account, text, message_id))
        self.db.commit()
        return {"id": cur.lastrowid, "ts": ts, "level": level, "account": account, "text": text, "message_id": message_id}

    def events(self, limit=200):
        return self._rows("SELECT * FROM events ORDER BY id DESC LIMIT ?", limit)

    def add_trade(self, account, key, action, symbol, side, volume, ticket, detail=""):
        self.db.execute("INSERT INTO trades(ts,account,signal_key,action,symbol,side,volume,ticket,detail) VALUES(?,?,?,?,?,?,?,?,?)",
                        (time.time(), account, key, action, symbol, side, volume, ticket, detail))
        self.db.commit()

    def trades(self, limit=200):
        return self._rows("SELECT * FROM trades ORDER BY id DESC LIMIT ?", limit)

    def trades_today_count(self, since_ts: float) -> dict:
        return {r["account"]: r["n"] for r in self._rows(
            "SELECT account, COUNT(DISTINCT signal_key) n FROM trades WHERE action='open' AND ts>=? GROUP BY account", since_ts)}

    # equity
    def add_equity(self, account, balance, equity):
        self.db.execute("INSERT INTO equity VALUES(?,?,?,?)", (time.time(), account, balance, equity))
        self.db.commit()

    def equity_series(self, since_ts: float) -> dict[str, list]:
        out: dict[str, list] = {}
        for r in self._rows("SELECT ts, account, equity FROM equity WHERE ts>=? ORDER BY ts", since_ts):
            out.setdefault(r["account"], []).append([r["ts"], r["equity"]])
        return out

    def delete_account(self, name):
        self.db.execute("DELETE FROM equity WHERE account=?", (name,))
        self.db.commit()
