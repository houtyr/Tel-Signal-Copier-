"""Copy engine: turns parsed Telegram messages into trades on every connected account,
tracks P&L, and enforces prop-firm limits. Emits events to the UI through `Bus`."""
from __future__ import annotations

import asyncio
import contextvars
import functools
import json
import logging
import multiprocessing as mp
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import worker
from risk import PropGuard, floor_step, size_position, split_volume, trading_day
from settings import Settings
from signal_parser import Command, Signal, parse
from store import Store
from interpreter import Interpreter, to_trade_objects
from mirror import MirrorMixin

ROOT = Path(__file__).parent
log = logging.getLogger("engine")
current_msg: contextvars.ContextVar[int | None] = contextvars.ContextVar("current_msg", default=None)
current_notes: contextvars.ContextVar[list | None] = contextvars.ContextVar("current_notes", default=None)


def describe_ai(ai: dict) -> str:
    if ai.get("signal"):
        s = ai["signal"]
        what = "add entries" if ai["kind"] == "add_entries" else "new trade"
        txt = f"{what}: {s['side'].upper()} {s['symbol'] or ''} @ {'/'.join(f'{e:g}' for e in s['entries']) or 'market'} SL {s['sl'] or '-'}"
        if s["tps"]:
            txt += f" TP {'/'.join(f'{t:g}' for t in s['tps'][:3])}"
        return txt
    parts = []
    for a in ai.get("actions", []):
        t = a["type"].replace("_", " ")
        if a.get("fraction"):
            t += f" {a['fraction']:.0%}"
        if a.get("level"):
            t += f" at {a['level'].replace('_', ' ')}"
        if a.get("price"):
            t += f" to {a['price']:g}"
        parts.append(t)
    return ", then ".join(parts) or ai.get("kind", "")


def _ago(ts: float) -> str:
    m = (time.time() - ts) / 60
    return "just now" if m < 1 else f"{m:.0f} min ago" if m < 90 else f"{m / 60:.1f} h ago"


class Bus:
    """Fan-out of live events to every open browser tab."""

    def __init__(self):
        self.queues: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q = asyncio.Queue(maxsize=500)
        self.queues.add(q)
        return q

    def unsubscribe(self, q):
        self.queues.discard(q)

    def emit(self, kind: str, data):
        msg = json.dumps({"type": kind, "data": data}, default=str)
        for q in list(self.queues):
            if not q.full():
                q.put_nowait(msg)


# ===================================================================== accounts
class Account:
    def __init__(self, name: str, settings: Settings):
        self.name = name
        self.settings = settings
        self.pool: ProcessPoolExecutor | None = None
        self.status = "disconnected"     # disconnected | connecting | connected | error | disabled
        self.error = ""
        self.info = None
        self.positions = []
        self.last_update = 0.0
        self.halted = ""
        self.retry_at = 0.0
        self.last_equity_write = 0.0

    @property
    def cfg(self) -> dict:
        return self.settings.account_runtime_cfg(self.name)

    @property
    def guard(self) -> PropGuard:
        c = self.cfg
        c.setdefault("initial_balance", c.get("starting_balance") or 100000)
        return PropGuard(c)

    def start_worker(self):
        c = self.cfg
        c["_state_dir"] = str(ROOT / "paper_state")
        self.pool = ProcessPoolExecutor(max_workers=1, mp_context=mp.get_context("spawn"),
                                        initializer=worker.init, initargs=(c["platform"], c, str(ROOT)))

    def stop_worker(self):
        if self.pool:
            self.pool.shutdown(wait=False, cancel_futures=True)
        self.pool = None

    async def call(self, method: str, *args, **kw):
        if not self.pool:
            raise RuntimeError("account not connected")
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(self.pool, functools.partial(worker.call, method, *args, **kw))
        except BrokenProcessPool:
            self.pool = None
            self.status, self.error = "error", "broker process crashed - reconnecting"
            raise

    def broker_symbol(self, symbol: str) -> str:
        c = self.cfg
        return (c.get("symbol_map") or {}).get(symbol, symbol + (c.get("symbol_suffix") or ""))

    def listens_to(self, chat_id: int) -> bool:
        chans = self.cfg.get("channels") or []
        return not chans or int(chat_id) in {int(c) for c in chans}

    def allowed(self, symbol: str) -> bool:
        c = self.cfg
        allow, block = c.get("allowed_symbols") or [], c.get("blocked_symbols") or []
        return symbol not in block and (not allow or symbol in allow)


# ===================================================================== engine
class Engine(MirrorMixin):
    def __init__(self, settings: Settings, store: Store, bus: Bus):
        self.settings = settings
        self.store = store
        self.bus = bus
        self.accounts: dict[str, Account] = {}
        self.state_path = ROOT / "state.json"
        self.state = {"signals": {}, "daily": {}, "links": {}, "mirror": {}}
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text())
        for k in ("signals", "daily", "links", "mirror"):
            self.state.setdefault(k, {})
        self._tasks: list[asyncio.Task] = []
        self.ai = Interpreter(settings)

    def save_state(self):
        # keep the file small: drop closed signals older than 3 days
        cutoff = time.time() - 3 * 86400
        self.state["signals"] = {k: v for k, v in self.state["signals"].items()
                                 if not (v.get("closed") and v["ts"] < cutoff)}
        self.state["links"] = {k: v for k, v in self.state.get("links", {}).items() if v in self.state["signals"]}
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1))
        tmp.replace(self.state_path)

    # ---------- notes (log lines tied to the message being processed) ----------
    def note(self, level: str, account: str | None, text: str):
        getattr(log, {"error": "error", "warn": "warning"}.get(level, "info"))("[%s] %s", account or "-", text)
        ev = self.store.add_event(level, account, text, current_msg.get())
        notes = current_notes.get()
        if notes is not None:
            notes.append({"level": level, "account": account, "text": text})
        self.bus.emit("event", ev)

    # ---------- lifecycle ----------
    async def start(self):
        for a in self.settings["accounts"]:
            acc = self.accounts.setdefault(a["name"], Account(a["name"], self.settings))
            if a.get("enabled", True):
                asyncio.create_task(self.connect(a["name"]))
            else:
                acc.status = "disabled"
        self._tasks.append(asyncio.create_task(self._poll_loop()))
        self._tasks.append(asyncio.create_task(self._mirror_loop()))

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        for a in self.accounts.values():
            a.stop_worker()

    async def connect(self, name: str):
        acc = self.accounts.setdefault(name, Account(name, self.settings))
        acc.stop_worker()
        acc.status, acc.error = "connecting", ""
        self.emit_account(acc)
        try:
            acc.start_worker()
            await acc.call("connect")
            acc.info = await acc.call("account")
            acc.positions = await acc.call("positions")
            acc.status = "connected"
            await self.day_start_equity(acc)
            self.note("info", name, f"connected - balance {acc.info.balance:,.2f} {acc.info.currency}")
        except Exception as e:
            acc.status, acc.error = "error", str(e)[:300]
            acc.retry_at = time.time() + 30
            acc.stop_worker()
            self.note("error", name, f"connect failed: {acc.error}")
        self.emit_account(acc)

    async def disconnect(self, name: str):
        acc = self.accounts.get(name)
        if acc:
            acc.stop_worker()
            acc.status = "disconnected"
            self.emit_account(acc)

    async def upsert_account(self, data: dict, original_name: str | None):
        old_name = original_name or data["name"]
        self.settings.upsert_account(data, original_name)     # validates first; raises before anything stops
        was = self.accounts.pop(old_name, None)
        if was:
            was.stop_worker()
        acc = Account(data["name"], self.settings)
        self.accounts[data["name"]] = acc
        if self.settings.account(data["name"]).get("enabled", True):
            await self.connect(data["name"])
        else:
            acc.status = "disabled"
            self.emit_account(acc)

    async def delete_account(self, name: str):
        acc = self.accounts.pop(name, None)
        if acc:
            acc.stop_worker()
        self.settings.delete_account(name)
        self.bus.emit("account_removed", name)

    # ---------- day tracking ----------
    def _today(self) -> str:
        r = self.settings["day_reset"]
        return trading_day(datetime.now(timezone.utc), r.get("timezone", "America/New_York"), int(r.get("hour", 17)))

    async def day_start_equity(self, acc: Account) -> float:
        day = self._today()
        d = self.state["daily"].get(acc.name)
        if not d or d["day"] != day:
            info = acc.info or await acc.call("account")
            d = {"day": day, "start_equity": max(info.balance, info.equity), "signals": 0}
            self.state["daily"][acc.name] = d
            if acc.halted:
                self.note("info", acc.name, "new trading day - copying resumed")
            acc.halted = ""
            self.save_state()
        return d["start_equity"]

    # ---------- snapshots for the UI ----------
    def account_view(self, acc: Account) -> dict:
        c = self.settings.account(acc.name) or {}
        cfg = acc.cfg if c else {}
        d = self.state["daily"].get(acc.name, {})
        v = {"name": acc.name, "platform": c.get("platform"), "enabled": c.get("enabled", True),
             "copy_enabled": c.get("copy_enabled", True), "status": acc.status, "error": acc.error,
             "halted": acc.halted, "channels": c.get("channels") or [], "last_update": acc.last_update,
             "risk_pct": cfg.get("risk_pct"), "risk_multiplier": cfg.get("risk_multiplier"),
             "fixed_lot": cfg.get("fixed_lot"),
             "daily_stop_pct": cfg.get("daily_stop_pct"), "total_stop_pct": cfg.get("total_stop_pct"),
             "initial_balance": float(cfg.get("initial_balance") or cfg.get("starting_balance") or 0) if c else 0,
             "signals_today": d.get("signals", 0), "day_start_equity": d.get("start_equity"),
             "mirror_from": c.get("mirror_from"), "mirror_enabled": c.get("mirror_enabled", True),
             "mirror_sizing": c.get("mirror_sizing"), "mirror_value": c.get("mirror_value"),
             "followers": [x["name"] for x in self.settings["accounts"] if x.get("mirror_from") == acc.name]}
        if acc.info:
            g = acc.guard
            start = d.get("start_equity") or acc.info.equity
            v.update(balance=acc.info.balance, equity=acc.info.equity, currency=acc.info.currency,
                     daily_pnl=acc.info.equity - start,
                     daily_loss_pct=g.daily_loss_pct(start, acc.info.equity),
                     total_dd_pct=g.total_dd_pct(acc.info.equity),
                     total_pnl=acc.info.equity - g.initial,
                     open_pnl=sum(p.profit for p in acc.positions))
        v["positions"] = [asdict(p) for p in acc.positions]
        return v

    def emit_account(self, acc: Account):
        self.bus.emit("account", self.account_view(acc))

    def snapshot(self) -> dict:
        return {"accounts": [self.account_view(a) for a in self.accounts.values()], "mirror_groups": self.mirror_groups()}

    # ---------- polling / watchdog ----------
    async def _poll_loop(self):
        while True:
            await asyncio.sleep(float(self.settings.get("poll_sec", 3)))
            await asyncio.gather(*(self._poll(a) for a in list(self.accounts.values())), return_exceptions=True)

    async def _poll(self, acc: Account):
        c = self.settings.account(acc.name)
        if not c or not c.get("enabled", True):
            return
        if acc.status == "error" and time.time() > acc.retry_at:
            await self.connect(acc.name)
            return
        if acc.status != "connected":
            return
        try:
            acc.info = await acc.call("account")
            acc.positions = await acc.call("positions")
            acc.last_update = time.time()
            start = await self.day_start_equity(acc)
            if time.time() - acc.last_equity_write > 30:
                self.store.add_equity(acc.name, acc.info.balance, acc.info.equity)
                acc.last_equity_write = time.time()
            await self._enforce_limits(acc, start)
            await self._breakeven_runners(acc)
        except Exception as e:
            acc.status, acc.error, acc.retry_at = "error", str(e)[:300], time.time() + 15
            self.note("error", acc.name, f"lost connection: {acc.error}")
        self.emit_account(acc)

    async def _enforce_limits(self, acc: Account, start: float):
        why = acc.guard.must_flatten(start, acc.info.equity)
        if why and not acc.halted:
            acc.halted = why
            self.note("error", acc.name, f"{why} hit at equity {acc.info.equity:,.2f} - halting new trades for today")
            if acc.cfg.get("flatten_on_stop", True) and not self.settings["dry_run"]:
                for p in await acc.call("positions"):
                    await acc.call("close", p.ticket)
                self.note("warn", acc.name, "closed all copier positions")

    async def _breakeven_runners(self, acc: Account):
        if not acc.cfg.get("breakeven_after_tp1", True):
            return
        for key, rec in self.state["signals"].items():
            refs = rec["legs"].get(acc.name, [])
            if rec.get("closed") or len(refs) < 2 or rec["be_done"].get(acc.name):
                continue
            if await acc.call("is_pending", refs[0]) or await acc.call("resolve", refs[0]) is not None:
                continue
            rest = [t for t in [await acc.call("resolve", r) for r in refs[1:]] if t]
            if not rest:
                rec["be_done"][acc.name] = True
                continue
            pos = {p.ticket: p for p in acc.positions}
            moved = 0
            for t in rest:
                p = pos.get(t)
                if p and p.profit > 0:
                    await acc.call("modify", t, sl=p.open_price)
                    moved += 1
            if moved:
                self.note("info", acc.name, f"TP1 filled on {rec['symbol']} - {moved} runner(s) moved to breakeven")
            rec["be_done"][acc.name] = True
            self.save_state()

    # ---------- incoming messages ----------
    def _reply_key(self, chat_id: int, reply_to: int | None) -> str | None:
        """The copied signal a reply refers to - directly, or via an earlier update that replied to it."""
        if not reply_to:
            return None
        key = f"{chat_id}:{reply_to}"
        if key in self.state["signals"]:
            return key
        k2 = self.state.get("links", {}).get(key)
        return k2 if k2 in self.state["signals"] else None

    def _ai_context(self, chat_id: int, reply_key: str | None) -> str:
        sigs = self.state["signals"]
        if reply_key:
            r = sigs[reply_key]
            legs = sum(len(v) for v in r["legs"].values())
            return (f"This message is a REPLY to this signal posted {_ago(r['ts'])}:\n{r.get('text', '')}\n"
                    f"(copier: {r['side']} {r['symbol']}, entry layers {r.get('layers') or [r.get('entry')]}, SL {r.get('sl')}, "
                    f"{legs} order(s) placed across accounts{', closed' if r.get('closed') else ''})")
        mine = [v for k, v in sigs.items() if k.startswith(f"{chat_id}:") and not v.get("closed")]
        if not mine:
            return "Not a reply. There are no open copied trades from this channel."
        lines = [f"- {v['side']} {v['symbol']} layers {v.get('layers') or [v.get('entry')]} SL {v.get('sl')} ({_ago(v['ts'])})" for v in mine[-5:]]
        return "Not a reply. Open copied trades from this channel:\n" + "\n".join(lines)

    async def on_message(self, chat_id: int, chat_title: str, msg_id: int, reply_to: int | None,
                         text: str, sent_at: datetime | None = None):
        aliases = {k.upper(): v.upper() for k, v in (self.settings.get("symbol_aliases") or {}).items()}
        parsed = parse(text, aliases)
        reply_key = self._reply_key(chat_id, reply_to)
        ask_ai = self.ai.should_ask(text, parsed, reply_key is not None)
        kind = "signal" if isinstance(parsed, Signal) else "command" if isinstance(parsed, list) else "none"
        stored = asdict(parsed) if isinstance(parsed, Signal) else [asdict(c) for c in parsed] if parsed else None
        mid = self.store.add_message(chat_id, chat_title, msg_id, reply_to, text, "ai" if ask_ai else kind, stored)
        if ask_ai:
            self.store.update_message(mid, status="thinking")
        self.bus.emit("message", self.store.message(mid))
        if parsed is None and not ask_ai:
            return

        notes: list = []
        current_msg.set(mid)
        current_notes.set(notes)
        status = "done"
        try:
            if ask_ai:
                ai = None
                try:
                    ai = await self.ai.interpret(text, self._ai_context(chat_id, reply_key))
                except Exception as e:
                    self.note("warn", None, f"AI interpreter unavailable ({str(e)[:120]}) - using the rules only")
                if ai:
                    sig, cmds = to_trade_objects(ai)
                    pct = f"{ai['confidence']:.0%}"
                    self.store.update_message(mid, kind="ai", parsed={"ai": ai, "rules": stored})
                    if ai["kind"] == "info":
                        self.note("info", None, f"AI: no action needed - {ai['reason']} ({pct} sure)")
                        status = "ignored"
                        parsed = None
                    else:
                        cfg = self.settings.ai()
                        needs_review = (ai["kind"] in ("new_signal", "add_entries") or not cfg.get("auto_manage", True)
                                        or ai["confidence"] < float(cfg.get("min_confidence", 0.8)))
                        if needs_review:
                            self.note("warn", None, f"AI suggests: {describe_ai(ai)} ({pct} sure) - waiting for your approval")
                            status = "review"
                            parsed = None
                        else:
                            self.note("info", None, f"AI read this as: {describe_ai(ai)} ({pct} sure)")
                            parsed = sig if sig else cmds
            if parsed is not None:
                status = await self._execute(chat_id, msg_id, reply_to, parsed, sent_at, text=text) or status
        except Exception as e:
            self.note("error", None, f"error: {e}")
            status = "error"
        if any(n["level"] == "error" for n in notes) and status == "done":
            status = "error"
        self.store.finish_message(mid, notes, status)
        self.bus.emit("message", self.store.message(mid))

    async def _execute(self, chat_id, msg_id, reply_to, parsed, sent_at, approved=False, text="") -> str | None:
        """Act on a parsed Signal or list of Commands. Returns a status override or None."""
        if not self.settings["copying_enabled"]:
            self.note("warn", None, "copying is paused - not acted on")
            return "skipped"
        if isinstance(parsed, Signal):
            if parsed.add_on or not parsed.symbol:
                return await self.add_entries(chat_id, msg_id, reply_to, parsed)
            age = (datetime.now(timezone.utc) - sent_at).total_seconds() if sent_at and not approved else 0
            max_age = float(self.settings.get("max_signal_age_sec", 60))
            if age > max_age:
                self.note("warn", None, f"signal is {age:.0f}s old (max {max_age:.0f}s) - skipped")
                return "skipped"
            await self.open_signal(chat_id, f"{chat_id}:{msg_id}", parsed, text)
            return None
        targets, how = self.find_targets(chat_id, reply_to, parsed)
        self.note("info", None, how)
        if not targets:
            return "skipped"
        for t in targets:
            for cmd in parsed:
                await self.apply_command(t, cmd)
            self.state.setdefault("links", {})[f"{chat_id}:{msg_id}"] = t
        self.save_state()
        return None

    async def apply_review(self, mid: int, approve: bool):
        """The user approved or dismissed an AI suggestion from the feed."""
        m = self.store.message(mid)
        if not m or m.get("status") != "review":
            raise ValueError("this message is not waiting for approval")
        notes = list(m.get("outcomes") or [])
        current_msg.set(mid)
        current_notes.set(notes)
        if not approve:
            self.note("info", None, "dismissed - no action taken")
            self.store.finish_message(mid, notes, "dismissed")
            self.bus.emit("message", self.store.message(mid))
            return
        ai = (m.get("parsed") or {}).get("ai")
        sig, cmds = to_trade_objects(ai)
        self.note("info", None, "approved by you")
        status = "done"
        try:
            status = await self._execute(m["chat_id"], m["msg_id"], m["reply_to"], sig if sig else cmds, None,
                                         approved=True, text=m["text"]) or "done"
        except Exception as e:
            self.note("error", None, f"error: {e}")
            status = "error"
        self.store.finish_message(mid, notes, status)
        self.bus.emit("message", self.store.message(mid))

    def find_targets(self, chat_id: int, reply_to: int | None, cmds: list[Command]) -> tuple[list[str], str]:
        """Work out which copied signal(s) a management message refers to.
        Returns (signal keys, explanation for the feed)."""
        sigs = self.state["signals"]
        links = self.state.get("links", {})
        if reply_to:
            key = f"{chat_id}:{reply_to}"
            key = key if key in sigs else links.get(key)        # reply to a reply ("TP1 hit" -> "close rest")
            if key in sigs:
                s = sigs[key]
                return [key], f"reply to the {s['side'].upper()} {s['symbol']} signal from {_ago(s['ts'])}"
            return [], "this is a reply to a message the copier didn't trade - nothing to manage"
        mode = self.settings.get("unlinked_commands", "only_if_one_open")
        if mode == "ignore":
            return [], "not a reply to a signal - ignored (setting: only act on replies)"
        symbol = next((c.symbol for c in cmds if c.symbol), None)
        mine = sorted((k for k, s in sigs.items() if k.startswith(f"{chat_id}:") and not s.get("closed")
                       and (not symbol or s["symbol"] == symbol)), key=lambda k: sigs[k]["ts"])
        what = f"open {symbol} trades" if symbol else "open trades"
        if not mine:
            return [], f"not a reply, and no {what} from this channel - nothing to do"
        if mode == "only_if_one_open" and len(mine) > 1:
            return [], (f"not a reply, and {len(mine)} {what} from this channel - skipped because it's unclear "
                        f"which one is meant (reply to the signal, or change this in Settings)")
        if symbol and mode == "symbol_or_last":
            return mine, f"not a reply - applied to all {len(mine)} {what} from this channel"
        s = sigs[mine[-1]]
        return mine[-1:], f"not a reply - applied to the latest {s['side'].upper()} {s['symbol']} signal ({_ago(s['ts'])})"

    # ---------- opening ----------
    async def open_signal(self, chat_id: int, key: str, sig: Signal, text: str = ""):
        if key in self.state["signals"]:
            return
        targets = []
        for a in self.accounts.values():
            c = self.settings.account(a.name) or {}
            if not c.get("enabled", True) or a.status != "connected":
                continue
            if not c.get("copy_enabled", True):
                continue
            if not a.listens_to(chat_id):
                continue
            targets.append(a)
        if not targets:
            self.note("warn", None, "no connected account is set to copy this channel")
            return
        record = {**asdict(sig), "ts": time.time(), "chat_id": chat_id, "text": text[:600], "legs": {}, "leg_layer": {},
                  "be_done": {}, "closed": False}
        results = await asyncio.gather(*(self.open_on_account(a, sig, key) for a in targets), return_exceptions=True)
        for a, res in zip(targets, results):
            if isinstance(res, Exception):
                self.note("error", a.name, f"failed to open: {res}")
            elif res and res[0]:
                record["legs"][a.name], record["leg_layer"][a.name] = res
        if record["legs"]:
            self.state["signals"][key] = record
            self.save_state()

    async def add_entries(self, chat_id: int, msg_id: int, reply_to: int | None, sig: Signal) -> str | None:
        """'Add more sell 4177//4183, same target, update SL 4193' - extra entries on the replied signal."""
        key = self._reply_key(chat_id, reply_to)
        if not key:
            self.note("info", None, "looks like 'add more entries', but it isn't a reply to a copied signal - skipped")
            return "skipped"
        rec = self.state["signals"][key]
        self.state.setdefault("links", {})[f"{chat_id}:{msg_id}"] = key
        mode = self.settings["risk"].get("add_on_entries", "half")
        if sig.side and sig.side != rec["side"]:
            self.note("warn", None, f"add-on is {sig.side} but the signal is {rec['side']} - skipped")
            return "skipped"
        # SL update applies to the existing positions too
        if sig.sl and sig.sl != rec.get("sl"):
            self.note("info", None, f"stop loss updated {rec.get('sl')} -> {sig.sl}")
            await self.apply_command(key, Command("move_sl", value=sig.sl))
            rec["sl"] = sig.sl
        if mode == "off":
            self.note("info", None, "add-on entries are turned off in Settings - only the SL update was applied")
            self.save_state()
            return None
        full = Signal(rec["symbol"], rec["side"], "market", sig.entry, sig.entry_range, sig.sl or rec.get("sl"),
                      sig.tps or rec.get("tps") or [], sig.layers or ([sig.entry] if sig.entry else []))
        factor = 0.5 if mode == "half" else 1.0
        base_layer = len(rec.get("layers") or [rec.get("entry")])
        for name in list(rec["legs"]):
            a = self.accounts.get(name)
            if not a or a.status != "connected":
                continue
            try:
                refs, layers = await self.open_on_account(a, full, key, risk_factor=factor, label="add-on")
                rec["legs"][name] += refs
                rec.setdefault("leg_layer", {}).setdefault(name, []).extend(l + base_layer for l in layers)
            except Exception as e:
                self.note("error", name, f"add-on failed: {e}")
        rec["layers"] = (rec.get("layers") or [rec.get("entry")]) + (full.layers or [full.entry])
        self.save_state()
        return None

    def _entry_plan(self, a: Account, sig: Signal, mkt: float, sl: float | None) -> list[tuple[int, str, float | None]]:
        """Decide how to enter: list of (layer index, order type, price). Notes explain skipped layers."""
        r = a.cfg
        buy = sig.side == "buy"
        layers = sig.layers if (sig.layers and r.get("zone_entry", "layers") == "layers") else []
        if not layers:
            if sig.order_type != "market":
                return [(0, sig.order_type, sig.entry)]
            layers = [sig.entry] if sig.entry is not None else []
            if not layers:
                return [(0, "market", None)]
        plan = []
        for i, lp in enumerate(layers):
            if sl is not None and ((buy and lp <= sl) or (not buy and lp >= sl)):
                self.note("info", a.name, f"entry layer {i + 1} ({lp:g}) is at/behind the SL - not used")
                continue
            already_better = (mkt <= lp) if buy else (mkt >= lp)
            if already_better:
                plan.append((i, "market", None))
                continue
            if i == 0 and len(plan) == 0:
                moved = (mkt - lp) if buy else (lp - mkt)
                limit = float(r.get("max_entry_slip_of_sl", 0.3)) * abs(lp - sl) if sl is not None else 0
                in_zone = sig.entry_range and sig.entry_range[0] <= mkt <= sig.entry_range[1]
                if in_zone or moved <= limit:
                    plan.append((i, "market", None))
                    continue
                action = r.get("if_price_moved", "limit")
                if action == "skip":
                    self.note("warn", a.name, f"price ran {moved:.5g} past entry {lp:g} - layer {i + 1} skipped")
                    continue
                if action == "market":
                    plan.append((i, "market", None))
                    continue
            plan.append((i, "limit", lp))
        return plan

    async def open_on_account(self, a: Account, sig: Signal, key: str, risk_factor: float = 1.0,
                              label: str = "") -> tuple[list[int], list[int]]:
        r = a.cfg
        dry = self.settings["dry_run"]
        if a.halted:
            self.note("warn", a.name, f"skipped - halted ({a.halted})"); return [], []
        if not a.allowed(sig.symbol):
            self.note("info", a.name, f"skipped - {sig.symbol} not allowed on this account"); return [], []
        if sig.order_type != "market" and sig.entry is None:
            self.note("warn", a.name, f"skipped - {sig.order_type} order without entry price"); return [], []
        sym = a.broker_symbol(sig.symbol)

        info = await a.call("account")
        start_eq = await self.day_start_equity(a)
        open_pos = await a.call("positions")
        reason = a.guard.check_new_trade(start_eq, info.equity, len(open_pos),
                                         self.state["daily"][a.name]["signals"] if not label else 0)
        if reason:
            self.note("warn", a.name, f"skipped - {reason}"); return [], []

        spec = await a.call("spec", sym)
        mkt = await a.call("price", sym, sig.side)

        sl = sig.sl
        if sl is None:
            dflt = (r.get("default_sl_distance") or {}).get(sig.symbol)
            if r.get("require_sl", True) and not dflt:
                self.note("warn", a.name, "skipped - no SL in signal"); return [], []
            if dflt:
                sl = mkt - float(dflt) if sig.side == "buy" else mkt + float(dflt)
        if sl is not None and ((sig.side == "buy" and mkt <= sl) or (sig.side == "sell" and mkt >= sl)):
            self.note("warn", a.name, f"skipped - price {mkt:.5g} already past SL {sl:.5g}"); return [], []

        plan = self._entry_plan(a, sig, mkt, sl)
        if not plan:
            self.note("warn", a.name, "skipped - no usable entry"); return [], []

        # ----- size: risk is spread over all planned layers (assuming every layer fills)
        if r.get("fixed_lot"):
            total = float(r["fixed_lot"]) * risk_factor
        else:
            if sl is None:
                self.note("warn", a.name, "skipped - % risk sizing needs an SL (set a fixed lot to trade without)"); return [], []
            base = {"initial": a.guard.initial, "balance": info.balance, "equity": info.equity}[r.get("risk_base", "initial")]
            risk_money = base * float(r.get("risk_pct", 0.5)) / 100 * float(r.get("risk_multiplier", 1.0)) * risk_factor
            avg_dist = sum(abs((mkt if t == "market" else p) - sl) for _, t, p in plan) / len(plan)
            total = size_position(risk_money, sl + avg_dist if sig.side == "buy" else sl - avg_dist, sl, spec)
        if total <= 0:
            self.note("warn", a.name, "skipped - minimum lot would exceed the risk budget"); return [], []
        while len(plan) > 1 and total / len(plan) < spec.volume_min:
            plan = plan[:-1]              # too small to split across every layer - drop the deepest
        per_layer = total / len(plan)

        tps_all = [t for t in sig.tps if (t > (sl or 0)) == (sig.side == "buy")]
        tps = tps_all[: int(r.get("max_tps", 3))] if r.get("split_tps", True) else tps_all[-1:]
        if r.get("use_final_tp_only") and tps_all:
            tps = [tps_all[-1]]

        refs, layers_out, placed = [], [], []
        for layer, otype, price in plan:
            vols = split_volume(floor_step(per_layer, spec.volume_step), max(1, len(tps)), spec)
            legs_tp = tps[: len(vols)] + [None] * (len(vols) - len(tps[: len(vols)]))
            for v, tp in zip(vols, legs_tp):
                if v < spec.volume_min:
                    continue
                if dry:
                    continue
                ref = await a.call("open", sym, sig.side, v, otype, price, sl, tp, f"tg {key}")
                refs.append(ref)
                layers_out.append(layer)
                self.store.add_trade(a.name, key, "open" if not label else label, sym, sig.side, v, ref,
                                     f"layer {layer + 1} {otype}{f' @ {price:g}' if price else ''} SL {sl} TP {tp}")
            placed.append(f"{'market' if otype == 'market' else f'limit {price:g}'}")
        desc = (f"{sig.side.upper()} {sym} {total:.2f} lots - {' + '.join(placed)}"
                f" ({len(tps) or 1} TP leg{'s' if len(tps) > 1 else ''} each) SL {sl:.5g}")
        self.note("ok", a.name, ("DRY RUN - would have placed " if dry else (f"{label}: " if label else "placed ")) + desc)
        if (refs or dry) and not label:
            self.state["daily"][a.name]["signals"] += 1
        return refs, layers_out

    # ---------- management ----------
    async def apply_command(self, key: str, cmd: Command):
        rec = self.state["signals"][key]
        for name, refs in rec["legs"].items():
            a = self.accounts.get(name)
            if not a or a.status != "connected" or not refs:
                continue
            try:
                await self.apply_on_account(a, key, rec, refs, cmd)
            except Exception as e:
                self.note("error", name, f"{cmd.action} failed: {e}")
        self.save_state()

    @staticmethod
    def _layer_price(rec: dict, ref: str | None) -> float | None:
        layers = [x for x in (rec.get("layers") or []) if x is not None]
        if not ref or not layers:
            return None
        return {"layer_low": min(layers), "layer_high": max(layers), "layer1": layers[0],
                "layer2": layers[1] if len(layers) > 1 else None}.get(ref)

    async def apply_on_account(self, a: Account, key: str, rec: dict, refs: list[int], cmd: Command):
        dry = self.settings["dry_run"]
        if cmd.action == "tp_hit":
            mode = a.cfg.get("tp_hit_action", "breakeven")
            n = int(cmd.value or 1)
            if mode == "none" or (mode == "breakeven" and not a.cfg.get("breakeven_after_tp1", True)):
                self.note("info", a.name, f"TP{n} hit - no action (setting)"); return
            if mode == "trail" and n >= 2 and len(rec.get("tps") or []) >= n - 1:
                cmd = Command("move_sl", ref=f"tp{n - 1}")
            elif rec["be_done"].get(a.name):
                self.note("info", a.name, f"TP{n} hit - already at breakeven"); return
            else:
                cmd = Command("breakeven")
        if cmd.action == "cancel":
            n = 0
            for r in refs:
                if not dry and await a.call("cancel_pending", r):
                    n += 1
            self.note("ok", a.name, f"cancelled {n} pending order(s)")
            return

        sl_target = cmd.value
        if cmd.action == "move_sl" and cmd.ref:
            i = int(cmd.ref[2:]) - 1
            if i >= len(rec.get("tps") or []):
                self.note("warn", a.name, f"signal has no {cmd.ref.upper()} - SL not moved"); return
            sl_target = rec["tps"][i]
        if cmd.action == "breakeven" and cmd.ref:
            sl_target = self._layer_price(rec, cmd.ref)
            if sl_target is None:
                self.note("info", a.name, f"signal has no {cmd.ref.replace('_', ' ')} - using each position's own entry")

        layer_of = dict(zip(refs, rec.get("leg_layer", {}).get(a.name) or [0] * len(refs)))
        live = {}
        for r in refs:
            t = await a.call("resolve", r)
            if t:
                live[t] = layer_of.get(r, 0)
        if cmd.action == "close_all" and not dry:
            for r in refs:                       # also remove deeper-layer limits that never filled
                if await a.call("is_pending", r):
                    await a.call("cancel_pending", r)
        if not live:
            self.note("info", a.name, "nothing open for this signal")
            if cmd.action == "close_all":
                rec["closed"] = True
            return
        pos = {p.ticket: p for p in await a.call("positions")}

        targets = list(live)
        if cmd.action == "close_worst":
            open_layers = {}
            for t, lay in live.items():
                p = pos.get(t)
                if p:
                    open_layers.setdefault(lay, []).append(p)
            if len(open_layers) < 2:
                self.note("info", a.name, "only one entry layer is open - nothing worse to close, kept")
                return
            avg = {lay: sum(p.open_price for p in ps) / len(ps) for lay, ps in open_layers.items()}
            worst = (max if rec["side"] == "buy" else min)(avg, key=avg.get)
            targets = [p.ticket for p in open_layers[worst]]

        skipped = 0
        for t in targets:
            p = pos.get(t)
            if dry:
                continue
            if cmd.action in ("close_all", "close_worst"):
                await a.call("close", t)
            elif cmd.action == "close_partial" and p:
                await a.call("close", t, p.volume * (cmd.value or 0.5))
            elif cmd.action in ("breakeven", "move_sl") and p:
                if cmd.action == "breakeven" and sl_target is None:
                    off = float(a.cfg.get("breakeven_offset", 0))
                    new_sl = p.open_price + off if p.side == "buy" else p.open_price - off
                else:
                    new_sl = sl_target
                exit_px = await a.call("price", p.symbol, "sell" if p.side == "buy" else "buy")
                # A stop on the wrong side of the market would be rejected (or fill instantly) - don't send it
                if (p.side == "buy" and new_sl >= exit_px) or (p.side == "sell" and new_sl <= exit_px):
                    self.note("warn", a.name, f"#{t}: SL {new_sl:.5g} would be past the current price {exit_px:.5g} - left unchanged")
                    skipped += 1
                    continue
                # Never loosen a stop that is already tighter
                if p.sl and ((p.side == "buy" and new_sl < p.sl) or (p.side == "sell" and new_sl > p.sl)) \
                        and cmd.action == "breakeven":
                    skipped += 1
                    continue
                await a.call("modify", t, sl=new_sl)
                if cmd.action == "breakeven":
                    rec["be_done"][a.name] = True
            elif cmd.action == "move_tp":
                await a.call("modify", t, tp=cmd.value)
            self.store.add_trade(a.name, key, cmd.action, rec["symbol"], rec["side"], p.volume if p else None, t)
        if cmd.action == "breakeven":
            where = "breakeven" if sl_target is None else f"{sl_target:g} ({(cmd.ref or '').replace('_', ' ')})"
            label = f"moved SL to {where} on"
        else:
            label = {"close_all": "closed", "close_worst": "closed the worst-priced entries:",
                     "close_partial": f"closed {int((cmd.value or .5) * 100)}% of",
                     "move_sl": f"moved SL to {sl_target} on", "move_tp": f"moved TP to {cmd.value} on"}[cmd.action]
        done = len(targets) - skipped
        if done:
            self.note("ok", a.name, f"{'DRY RUN - would have ' if dry else ''}{label} {done} position(s)")
        elif skipped and cmd.action == "breakeven":
            self.note("info", a.name, "stops already at or beyond breakeven - unchanged")
        if cmd.action == "close_all":
            rec["closed"] = True

    # ---------- manual actions from the UI ----------
    async def close_position(self, name: str, ticket: int, volume: float | None = None):
        a = self.accounts[name]
        await a.call("close", int(ticket), float(volume) if volume else None)
        self.note("ok", name, f"manually closed {f'{float(volume):.2f} lots of ' if volume else ''}#{ticket}")
        await self._poll(a)

    async def close_all(self, name: str):
        a = self.accounts[name]
        n = 0
        for p in await a.call("positions"):
            await a.call("close", p.ticket)
            n += 1
        self.note("ok", name, f"manually closed all ({n}) copier positions")
        await self._poll(a)

    async def flatten_everything(self):
        for name, a in self.accounts.items():
            if a.status == "connected":
                try:
                    await self.close_all(name)
                except Exception as e:
                    self.note("error", name, f"close all failed: {e}")
