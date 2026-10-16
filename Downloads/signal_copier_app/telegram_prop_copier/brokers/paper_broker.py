"""Paper (simulated) account. Prices move in smooth waves around realistic levels; SL/TP fill automatically.
Use it to try the app, test a channel's signals, or rehearse before connecting a real prop account."""
from __future__ import annotations

import math
import time
import json
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from .base import AccountInfo, Broker, Position, SymbolSpec

BASE = {"XAUUSD": 4140.0, "XAGUSD": 29.5, "EURUSD": 1.0850, "GBPUSD": 1.2700, "USDJPY": 151.20,
        "AUDUSD": 0.6600, "USDCAD": 1.3650, "US30": 39250.0, "NAS100": 18200.0, "SPX500": 5250.0,
        "GER40": 18400.0, "BTCUSD": 64000.0, "ETHUSD": 3100.0, "USOIL": 78.0}


class PaperBroker(Broker):
    def __init__(self, cfg: dict):
        self.name = cfg["name"]
        self.cfg = cfg
        self.balance = float(cfg.get("starting_balance") or cfg.get("initial_balance") or 100000)
        self.pos: dict[int, Position] = {}
        self.pending: dict[int, dict] = {}
        self.next_id = 1000
        sd = cfg.get("_state_dir")
        self.state_file = Path(sd) / f"{''.join(c if c.isalnum() else '_' for c in self.name)}.json" if sd else None
        self._load()

    # ---------- persistence (so paper trades survive an app restart) ----------
    def _load(self):
        if self.state_file and self.state_file.exists():
            d = json.loads(self.state_file.read_text())
            self.balance, self.next_id = d["balance"], d["next_id"]
            self.pos = {int(k): Position(**v) for k, v in d["pos"].items()}
            self.pending = {int(k): v for k, v in d["pending"].items()}

    def _save(self):
        if self.state_file:
            self.state_file.parent.mkdir(exist_ok=True)
            self.state_file.write_text(json.dumps({"balance": self.balance, "next_id": self.next_id,
                                                   "pos": {k: asdict(v) for k, v in self.pos.items()},
                                                   "pending": self.pending}))

    def connect(self) -> None:
        pass

    # ---------- market simulation ----------
    def _base(self, symbol: str) -> str:
        for k in BASE:
            if symbol.upper().startswith(k):
                return k
        return symbol

    def _mid(self, symbol: str) -> float:
        """Deterministic function of time, so every paper account sees the same market."""
        b = self._base(symbol)
        base = BASE.get(b, 100.0)
        ph = sum(map(ord, b)) % 97
        t = time.time()
        wave = (0.0016 * math.sin(t / 610 + ph) + 0.0009 * math.sin(t / 97 + ph * 1.7)
                + 0.0004 * math.sin(t / 23 + ph * 2.3) + 0.00015 * math.sin(t / 3.1 + ph))
        return base * (1 + wave)

    def _spread(self, symbol: str) -> float:
        return self._mid(symbol) * 0.00004

    def price(self, symbol: str, side: str) -> float:
        m, s = self._mid(symbol), self._spread(symbol) / 2
        return m + s if side == "buy" else m - s

    def spec(self, symbol: str) -> SymbolSpec:
        b = self._base(symbol)
        if b in ("XAUUSD",):
            v = 100.0
        elif b == "XAGUSD":
            v = 5000.0
        elif b == "USOIL":
            v = 1000.0
        elif len(b) == 6 and b[3:] == "USD":
            v = 100000.0
        elif len(b) == 6 and b[:3] == "USD":
            v = 100000.0 / self._mid(symbol)
        else:
            v = 1.0
        digits = 2 if self._mid(symbol) > 50 else 5 if self._mid(symbol) < 10 else 3
        return SymbolSpec(symbol, 0.01, 100.0, 0.01, v, digits)

    def _pl(self, p: Position) -> float:
        exit_px = self.price(p.symbol, "sell" if p.side == "buy" else "buy")
        diff = (exit_px - p.open_price) if p.side == "buy" else (p.open_price - exit_px)
        return diff * p.volume * self.spec(p.symbol).value_per_price_unit

    def _tick(self):
        """Fill pending orders and SL/TP hits."""
        for oid, o in list(self.pending.items()):
            px = self.price(o["symbol"], o["side"])
            hit = (o["type"] == "limit" and ((o["side"] == "buy" and px <= o["entry"]) or (o["side"] == "sell" and px >= o["entry"]))) or \
                  (o["type"] == "stop" and ((o["side"] == "buy" and px >= o["entry"]) or (o["side"] == "sell" and px <= o["entry"])))
            if hit:
                del self.pending[oid]
                self.pos[oid] = Position(oid, o["symbol"], o["side"], o["volume"], px, o["sl"], o["tp"])
                self._save()
        for t, p in list(self.pos.items()):
            px = self.price(p.symbol, "sell" if p.side == "buy" else "buy")
            if p.side == "buy":
                stop = p.sl is not None and px <= p.sl
                take = p.tp is not None and px >= p.tp
            else:
                stop = p.sl is not None and px >= p.sl
                take = p.tp is not None and px <= p.tp
            if stop or take:
                self.close(t)

    # ---------- account ----------
    def account(self) -> AccountInfo:
        self._tick()
        return AccountInfo(self.balance, self.balance + sum(self._pl(p) for p in self.pos.values()), "USD")

    def open(self, symbol, side, volume, order_type, entry, sl, tp, comment) -> int:
        self.next_id += 1
        oid = self.next_id
        if order_type == "market":
            self.pos[oid] = Position(oid, symbol, side, float(volume), self.price(symbol, side), sl, tp)
        else:
            self.pending[oid] = dict(symbol=symbol, side=side, volume=float(volume), type=order_type,
                                     entry=entry, sl=sl, tp=tp)
        self._save()
        return oid

    def positions(self) -> list[Position]:
        self._tick()
        out = []
        for p in self.pos.values():
            out.append(Position(p.ticket, p.symbol, p.side, p.volume, p.open_price, p.sl, p.tp,
                                round(self._pl(p), 2), self.price(p.symbol, "sell" if p.side == "buy" else "buy")))
        return out

    def resolve(self, ref: int) -> Optional[int]:
        self._tick()
        return ref if ref in self.pos else None

    def is_pending(self, ref: int) -> bool:
        return ref in self.pending

    def modify(self, ticket, sl=None, tp=None) -> bool:
        if ticket in self.pos:
            p = self.pos[ticket]
        elif ticket in self.pending:
            o = self.pending[ticket]
            o["sl"] = sl if sl is not None else o["sl"]
            o["tp"] = tp if tp is not None else o["tp"]
            self._save()
            return True
        else:
            return False
        if sl is not None:
            p.sl = sl
        if tp is not None:
            p.tp = tp
        self._save()
        return True

    def close(self, ticket, volume=None) -> bool:
        p = self.pos.get(ticket)
        if not p:
            return False
        vol = p.volume if volume is None else min(p.volume, max(0.01, math.floor(volume * 100) / 100))
        part = Position(p.ticket, p.symbol, p.side, vol, p.open_price, p.sl, p.tp)
        self.balance += self._pl(part)
        p.volume = round(p.volume - vol, 2)
        if p.volume <= 0:
            del self.pos[ticket]
        self._save()
        return True

    def cancel_pending(self, ref) -> bool:
        ok = self.pending.pop(ref, None) is not None
        self._save()
        return ok
