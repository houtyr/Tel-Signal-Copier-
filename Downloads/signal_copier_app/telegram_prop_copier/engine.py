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
MANAGE_SEC = 5          # how often open signals are checked for breakeven / trailing / stale limits
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
        self.paused_until = 0.0          # broker asked us to slow down (HTTP 429) - skip polls until then
        self.last_rate_note = 0.0
        self.last_manage = 0.0
        self.last_manage_note = 0.0
        self.digits: dict[str, int] = {}   # broker symbol -> price digits, for rounding stop levels

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
        for attempt in range(5):     # OneDrive / antivirus can hold the file for a moment ("Access is denied")
            try:
                tmp.write_text(json.dumps(self.state, indent=1))
                tmp.replace(self.state_path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.2)

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

    @staticmethod
    def is_rate_limit(err: Exception) -> bool:
        s = str(err)
        return "429" in s or "Too Many Requests" in s or "rate limit" in s.lower()

    def rate_limit_pause(self, acc: Account, err: Exception) -> bool:
        """A 429 from the broker isn't a lost connection - reconnecting would log in again and send even
        more requests. Pause this account's polling for a while instead. Returns False for other errors."""
        if not self.is_rate_limit(err):
            return False
        acc.paused_until = time.time() + 15
        if time.time() - acc.last_rate_note > 60:
            acc.last_rate_note = time.time()
            self.note("warn", acc.name, "broker is rate-limiting requests (429) - polling paused for 15s, still connected")
        return True

    async def _poll(self, acc: Account):
        c = self.settings.account(acc.name)
        if not c or not c.get("enabled", True):
            return
        if time.time() < acc.paused_until:
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
            try:
                await self._manage_signals(acc)
            except Exception as e:
                if self.is_rate_limit(e):
                    raise
                if time.time() - acc.last_manage_note > 300:     # a management hiccup isn't a lost connection
                    acc.last_manage_note = time.time()
                    self.note("warn", acc.name, f"trade management check failed: {str(e)[:200]}")
        except Exception as e:
            if not self.rate_limit_pause(acc, e):
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

    # ---------- trade management: stale pending orders, breakeven, trailing ----------
    # Per account, rec["st"][account][order ref] = {"s": pending|open|closed, "t": ticket, "sl": stop we last set,
    # "R": entry-to-SL distance, "best"/"worst": furthest the price went for/against the position (price units)}.
    async def _manage_signals(self, acc: Account):
        if time.time() - acc.last_manage < MANAGE_SEC:
            return
        acc.last_manage = time.time()
        c, dry, now, changed = acc.cfg, self.settings["dry_run"], time.time(), False
        prices: dict[tuple, float] = {}
        pos = {p.ticket: p for p in acc.positions}
        for key, rec in list(self.state["signals"].items()):
            refs = rec["legs"].get(acc.name)
            if rec.get("closed") or not refs or rec.setdefault("done", {}).get(acc.name):
                continue
            legs = rec.setdefault("st", {}).setdefault(acc.name, {})
            todo = [r for r in refs if legs.get(str(r), {}).get("s") != "closed"]
            states = await acc.call("leg_states", todo) if todo else {}
            for r in todo:
                L, s = legs.setdefault(str(r), {"s": "new", "miss": 0}), states.get(r)
                if s == "pending":
                    L["s"] = "pending"
                elif s:
                    if L["s"] != "open":
                        changed = True
                    L.update(s="open", t=s, miss=0)
                else:
                    # gone: filled and closed, or cancelled. Order history can lag a fresh fill, so confirm twice.
                    L["miss"] = L.get("miss", 0) + 1
                    if L["miss"] >= 2 and now - rec["ts"] > 30:
                        L["s"], changed = "closed", True
                    elif L["s"] == "pending":
                        L["s"] = "gone?"      # was pending, now isn't - filled or cancelled; never re-cancel it
            # only orders the broker reported as pending on this check get cancelled
            pending = [r for r in todo if states.get(r) == "pending"]
            live = [(r, legs[str(r)]) for r in refs if legs[str(r)]["s"] == "open"]
            if not pending and not live:
                if all(legs[str(r)]["s"] == "closed" for r in refs):
                    rec["done"][acc.name] = True
                    self._note_finished(acc, rec, legs)
                    if all(rec["done"].get(n) for n in rec["legs"]):
                        rec["closed"] = True
                    changed = True
                continue

            buy = rec["side"] == "buy"
            d = 1 if buy else -1
            sym = acc.broker_symbol(rec["symbol"])
            if (sym, buy) not in prices:            # the price a position would close at (bid for buys)
                prices[(sym, buy)] = await acc.call("price", sym, "sell" if buy else "buy")
            px = prices[(sym, buy)]
            tps = sorted((t for t in rec.get("tps") or [] if t), key=lambda t: t * d)

            if pending:
                gate = rec.setdefault("gate", {})
                if acc.name not in gate:     # first TP still ahead of the market when the signal came in
                    gate[acc.name] = next((t for t in tps if (t - px) * d > 0), None)
                why = None
                exp = float(c.get("pending_expiry_min") or 0)
                if exp and now - rec["ts"] >= exp * 60:
                    why = f"not filled within {exp:g} min"
                elif c.get("cancel_pending_at_tp1", True) and gate[acc.name] is not None \
                        and (px - gate[acc.name]) * d >= 0:
                    why = f"price already reached {gate[acc.name]:g} - the move happened without us, not chasing it"
                if why:
                    await self._cancel_refs(acc, rec, pending, why)
                    changed = True

            changed |= await self._protect(acc, key, rec, live, pos, px, tps, d)
        if changed:
            self.save_state()

    async def _protect(self, acc: Account, key: str, rec: dict, live: list, pos: dict, px: float,
                       tps: list[float], d: int) -> bool:
        """Breakeven, then lock profit TP by TP, then trail - all measured from each position's own fill."""
        c, changed = acc.cfg, False
        be_on = c.get("breakeven_after_tp1", True)
        be_at, lock = float(c.get("be_trigger_r") or 0), float(c.get("be_lock_r") or 0)
        tr_start, tr_dist = float(c.get("trail_start_r") or 0), float(c.get("trail_distance_r") or 0)
        for r, L in live:
            p = pos.get(L["t"])
            if not p or rec.get("sl") is None:
                continue
            e = p.open_price
            R = L.setdefault("R", abs(e - rec["sl"]))
            L.setdefault("sl", rec["sl"])
            if R <= 0:
                continue
            prog = (px - e) * d
            L["best"], L["worst"] = max(L.get("best", prog), prog), min(L.get("worst", prog), prog)
            best_px = e + d * L["best"]
            want, why = None, ""

            def tighter(level, label):
                nonlocal want, why
                if want is None or (level - want) * d > 0:
                    want, why = level, label

            if be_on and L["best"] >= be_at * R:
                tighter(e + d * lock * R, "breakeven" + (f" +{lock:g}R" if lock else ""))
            if c.get("trail_tp_ladder", True):
                reached = [t for t in tps if (best_px - t) * d >= 0]
                if len(reached) >= 2 and (reached[-2] - e) * d > lock * R:
                    tighter(reached[-2], f"locked at {reached[-2]:g} (price reached {reached[-1]:g})")
            if tr_start and tr_dist and L["best"] >= tr_start * R:
                tighter(best_px - d * tr_dist * R, f"trailing {tr_dist:g}R behind the best price")
            if want is None or (want - L["sl"]) * d <= 0.02 * R:      # nothing new, or too small to bother
                continue
            if (px - want) * d <= 0.02 * R:
                continue          # price has pulled back past the level - a stop there would be rejected; retry later
            want = await self._round_px(acc, p.symbol, want)
            if not self.settings["dry_run"]:
                await acc.call("modify", p.ticket, sl=want)
            L["sl"], changed = want, True
            if why.startswith("breakeven"):
                rec.setdefault("be_done", {})[acc.name] = True
            self.store.add_trade(acc.name, key, "auto_sl", rec["symbol"], rec["side"], p.volume, p.ticket, f"SL {want:g} - {why}")
            self.note("info", acc.name, f"{'DRY RUN - would move' if self.settings['dry_run'] else 'moved'} "
                                        f"SL of #{p.ticket} ({rec['symbol']}) to {want:g} - {why}")
        return changed

    @staticmethod
    async def _round_px(a: Account, sym: str, x: float) -> float:
        if sym not in a.digits:
            a.digits[sym] = (await a.call("spec", sym)).digits
        return round(x, a.digits[sym])

    async def _cancel_refs(self, a: Account, rec: dict, refs: list[int], why: str) -> int:
        legs = rec.setdefault("st", {}).setdefault(a.name, {})
        for r in refs:
            if not self.settings["dry_run"]:
                await a.call("cancel_pending", r)
            legs.setdefault(str(r), {})["s"] = "closed"
        if refs:
            self.note("info", a.name, f"cancelled {len(refs)} unfilled order(s) of the {rec['side'].upper()} "
                                      f"{rec['symbol']} signal - {why}")
        return len(refs)

    async def _cancel_pending(self, a: Account, rec: dict, why: str) -> int:
        """Cancel whatever of this signal is still an unfilled limit on account `a`."""
        legs = rec.setdefault("st", {}).setdefault(a.name, {})
        refs = [r for r in rec["legs"].get(a.name, []) if legs.get(str(r), {}).get("s") != "closed"]
        states = await a.call("leg_states", refs) if refs else {}
        return await self._cancel_refs(a, rec, [r for r in refs if states.get(r) == "pending"], why)

    def _note_finished(self, acc: Account, rec: dict, legs: dict):
        filled = [L for L in legs.values() if L.get("R")]
        if not filled:
            return
        best = max(L.get("best", 0) / L["R"] for L in filled)
        worst = min(L.get("worst", 0) / L["R"] for L in filled)
        self.note("info", acc.name, f"{rec['side'].upper()} {rec['symbol']} signal finished - price went up to "
                                    f"{best:+.2f}R in favour and {worst:+.2f}R against (R = entry-to-SL distance)")

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

        if not label:
            # A fresh signal on the same symbol replaces the channel's older idea: drop its unfilled limits
            chat = key.split(":")[0]
            older = [(k, s) for k, s in self.state["signals"].items() if k != key and s["symbol"] == sig.symbol
                     and not s.get("closed") and a.name in s["legs"] and not s.get("done", {}).get(a.name)]
            if r.get("cancel_pending_on_new_signal", True):
                for k, s in older:
                    if k.split(":")[0] == chat:
                        await self._cancel_pending(a, s, "a newer signal on the same symbol arrived")
            limit = int(r.get("max_signals_per_symbol") or 0)
            active = [k for k, s in older if not s.get("st", {}).get(a.name)
                      or any(L.get("s") != "closed" for L in s["st"][a.name].values())]
            if limit and len(active) >= limit:
                self.note("warn", a.name, f"skipped - already {len(active)} open {sig.symbol} signal(s) "
                                          f"(max {limit} per symbol)"); return [], []

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

        buy = sig.side == "buy"
        tps_all = [t for t in sig.tps if (t > (sl or 0)) == buy]

        def layer_tps(entry_px: float) -> tuple[list, list[str]]:
            """TP per leg for an entry at entry_px (None = runner without TP), plus notes on dropped TPs."""
            # A TP the price has already passed would be rejected or close instantly - drop it,
            # its share of the volume goes to the remaining TPs
            ok = [t for t in tps_all if (t > entry_px) == buy]
            why = []
            if len(ok) < len(tps_all):
                why.append(f"TP {', '.join(f'{t:g}' for t in tps_all if t not in ok)} already past the entry {entry_px:.5g}")
            R = abs(entry_px - sl) if sl is not None else 0
            near = [t for t in ok if abs(t - entry_px) < float(r.get("min_tp_distance_r") or 0) * R]
            if near and len(near) < len(ok):    # a TP a few ticks away is noise - its leg rides to the next TP
                ok = [t for t in ok if t not in near]
                why.append(f"TP {', '.join(f'{t:g}' for t in near)} too close to the entry {entry_px:.5g}")
            if r.get("use_final_tp_only") and ok:
                return [ok[-1]], why
            if not r.get("split_tps", True):
                return ok[-1:], why
            n, mode = int(r.get("max_tps", 3)), r.get("runner_tp", "last")
            if n < 2 or not ok or mode not in ("last", "none") or (mode == "last" and len(ok) <= n):
                return ok[:n], why
            if mode == "none":                  # last leg has no TP - the trailing stop decides when it exits
                return ok[:n - 1] + [None], why
            far = [t for t in ok[n - 1:] if not R or abs(t - entry_px) <= float(r.get("runner_max_r") or 3) * R]
            return ok[:n - 1] + [far[-1] if far else ok[n - 1]], why

        refs, layers_out, placed, leg_counts = [], [], [], []
        for layer, otype, price in plan:
            entry_px = mkt if otype == "market" else price
            tps, why = layer_tps(entry_px)
            if tps_all and not tps:
                self.note("warn", a.name, f"layer {layer + 1}: every TP is already past the entry {entry_px:.5g} - not placed")
                continue
            if why and r.get("split_tps", True) and not r.get("use_final_tp_only"):
                self.note("info", a.name, f"layer {layer + 1}: {'; '.join(why)} - skipped")
            leg_counts.append(len(tps) or 1)
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
        if not placed:
            self.note("warn", a.name, "skipped - every TP is already past the entry price"); return [], []
        n_legs = max(leg_counts)
        desc = (f"{sig.side.upper()} {sym} {total:.2f} lots - {' + '.join(placed)}"
                f" ({n_legs} TP leg{'s' if n_legs > 1 else ''} each) SL {sl:.5g}")
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
            for attempt in (1, 2):
                try:
                    await self.apply_on_account(a, key, rec, refs, cmd)
                except Exception as e:
                    # A 429 means the request was refused, not executed - wait out the broker's pause and redo it.
                    # Not for partial closes: repeating one after a half-done first try would close too much.
                    if attempt == 1 and self.is_rate_limit(e) and cmd.action != "close_partial":
                        self.note("warn", name, f"broker is rate-limiting - retrying {cmd.action.replace('_', ' ')} in 12s")
                        await asyncio.sleep(12)
                        continue
                    self.note("error", name, f"{cmd.action} failed: {e}")
                break
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
            if a.cfg.get("cancel_pending_at_tp1", True):
                await self._cancel_pending(a, rec, f"the channel says TP{n} hit - not chasing the move")
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
        live, ref_of = {}, {}
        for r in refs:
            t = await a.call("resolve", r)
            if t:
                live[t], ref_of[t] = layer_of.get(r, 0), r
        tracked = rec.setdefault("st", {}).setdefault(a.name, {})     # stops the copier set (TradeLocker doesn't report them)
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
                    # a hair into profit, so spread + commission don't turn "breakeven" into a small loss
                    R = abs(p.open_price - rec["sl"]) if rec.get("sl") is not None else 0
                    off = float(a.cfg.get("breakeven_offset", 0)) + float(a.cfg.get("be_lock_r") or 0) * R
                    new_sl = p.open_price + off if p.side == "buy" else p.open_price - off
                else:
                    new_sl = sl_target
                new_sl = await self._round_px(a, p.symbol, new_sl)
                L = tracked.setdefault(str(ref_of[t]), {"s": "open", "t": t})
                cur_sl = p.sl or L.get("sl") or rec.get("sl")
                exit_px = await a.call("price", p.symbol, "sell" if p.side == "buy" else "buy")
                # A stop on the wrong side of the market would be rejected (or fill instantly) - don't send it
                if (p.side == "buy" and new_sl >= exit_px) or (p.side == "sell" and new_sl <= exit_px):
                    self.note("warn", a.name, f"#{t}: SL {new_sl:.5g} would be past the current price {exit_px:.5g} - left unchanged")
                    skipped += 1
                    continue
                # Never loosen a stop that is already tighter (an explicit "SL to <price>" from the channel still applies)
                if cur_sl and ((p.side == "buy" and new_sl <= cur_sl) or (p.side == "sell" and new_sl >= cur_sl)) \
                        and (cmd.action == "breakeven" or cmd.ref):
                    skipped += 1
                    continue
                await a.call("modify", t, sl=new_sl)
                L["sl"] = new_sl
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
