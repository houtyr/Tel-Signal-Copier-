"""MetaTrader 5 adapter. Requires Windows + the MT5 terminal installed (pip install MetaTrader5).
One MT5 terminal = one account per process. For two MT5 accounts, install the terminal twice
(different folders) and point each account's `terminal_path` at its own terminal64.exe."""
from __future__ import annotations

from typing import Optional

from .base import AccountInfo, Broker, Position, SymbolSpec

try:
    import MetaTrader5 as mt5
except ImportError:  # lets the rest of the project import on non-Windows machines
    mt5 = None


class MT5Broker(Broker):
    def __init__(self, cfg: dict):
        self.name = cfg["name"]
        self.cfg = cfg
        self.magic = int(cfg.get("magic", 26100701))
        self.deviation = int(cfg.get("max_slippage_points", 30))

    # ---------- connection ----------
    def connect(self) -> None:
        if mt5 is None:
            raise RuntimeError("MetaTrader5 package not installed (Windows only): pip install MetaTrader5")
        kw = {}
        if self.cfg.get("terminal_path"):
            kw["path"] = self.cfg["terminal_path"]
        ok = mt5.initialize(login=int(self.cfg["login"]), password=self.cfg["password"],
                            server=self.cfg["server"], **kw)
        if not ok:
            raise RuntimeError(f"[{self.name}] MT5 initialize failed: {mt5.last_error()}")
        if not mt5.terminal_info().trade_allowed:
            raise RuntimeError(f"[{self.name}] Algo Trading is disabled in the MT5 terminal - enable the button.")

    def account(self) -> AccountInfo:
        a = mt5.account_info()
        return AccountInfo(a.balance, a.equity, a.currency)

    # ---------- symbols / prices ----------
    def _info(self, symbol: str):
        if not mt5.symbol_select(symbol, True):
            raise ValueError(f"[{self.name}] symbol {symbol} not found on this server")
        return mt5.symbol_info(symbol)

    def spec(self, symbol: str) -> SymbolSpec:
        i = self._info(symbol)
        return SymbolSpec(symbol, i.volume_min, i.volume_max, i.volume_step,
                          i.trade_tick_value / i.trade_tick_size, i.digits)

    def price(self, symbol: str, side: str) -> float:
        self._info(symbol)
        t = mt5.symbol_info_tick(symbol)
        return t.ask if side == "buy" else t.bid

    def _filling(self, symbol: str) -> int:
        fm = self._info(symbol).filling_mode
        if fm & 1:
            return mt5.ORDER_FILLING_FOK
        if fm & 2:
            return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN

    def _send(self, req: dict):
        res = mt5.order_send(req)
        if res is None or res.retcode not in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
            raise RuntimeError(f"[{self.name}] order_send failed: {getattr(res, 'retcode', None)} "
                               f"{getattr(res, 'comment', mt5.last_error())}")
        return res

    # ---------- trading ----------
    def open(self, symbol, side, volume, order_type, entry, sl, tp, comment) -> int:
        digits = self._info(symbol).digits
        req = {
            "symbol": symbol, "volume": float(volume), "magic": self.magic,
            "comment": comment[:31], "deviation": self.deviation,
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": self._filling(symbol),
        }
        if sl:
            req["sl"] = round(sl, digits)
        if tp:
            req["tp"] = round(tp, digits)
        if order_type == "market":
            req.update(action=mt5.TRADE_ACTION_DEAL, price=self.price(symbol, side),
                       type=mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL)
        else:
            types = {("buy", "limit"): mt5.ORDER_TYPE_BUY_LIMIT, ("sell", "limit"): mt5.ORDER_TYPE_SELL_LIMIT,
                     ("buy", "stop"): mt5.ORDER_TYPE_BUY_STOP, ("sell", "stop"): mt5.ORDER_TYPE_SELL_STOP}
            req.update(action=mt5.TRADE_ACTION_PENDING, price=round(entry, digits), type=types[(side, order_type)],
                       type_filling=mt5.ORDER_FILLING_RETURN)
        res = self._send(req)
        return int(res.order)  # in MT5 the position ticket equals the opening order ticket

    def positions(self) -> list[Position]:
        out = []
        for p in mt5.positions_get() or []:
            if p.magic != self.magic:
                continue
            out.append(Position(p.ticket, p.symbol, "buy" if p.type == mt5.POSITION_TYPE_BUY else "sell",
                                p.volume, p.price_open, p.sl or None, p.tp or None, p.profit, p.price_current))
        return out

    def all_positions(self) -> list[Position]:
        return [Position(p.ticket, p.symbol, "buy" if p.type == mt5.POSITION_TYPE_BUY else "sell", p.volume,
                         p.price_open, p.sl or None, p.tp or None, p.profit, p.price_current)
                for p in mt5.positions_get() or []]

    def resolve(self, ref: int) -> Optional[int]:
        return ref if mt5.positions_get(ticket=ref) else None

    def modify(self, ticket, sl=None, tp=None) -> bool:
        p = mt5.positions_get(ticket=ticket)
        if not p:
            # still a pending order? modify the order instead
            o = mt5.orders_get(ticket=ticket)
            if not o:
                return False
            o = o[0]
            self._send({"action": mt5.TRADE_ACTION_MODIFY, "order": ticket, "price": o.price_open,
                        "sl": sl if sl is not None else o.sl, "tp": tp if tp is not None else o.tp,
                        "type_time": mt5.ORDER_TIME_GTC})
            return True
        p = p[0]
        self._send({"action": mt5.TRADE_ACTION_SLTP, "position": ticket, "symbol": p.symbol,
                    "sl": sl if sl is not None else p.sl, "tp": tp if tp is not None else p.tp,
                    "magic": self.magic})
        return True

    def close(self, ticket, volume=None) -> bool:
        p = mt5.positions_get(ticket=ticket)
        if not p:
            return False
        p = p[0]
        spec = self.spec(p.symbol)
        vol = p.volume if volume is None else max(spec.volume_min, round(volume / spec.volume_step) * spec.volume_step)
        vol = min(vol, p.volume)
        side = "sell" if p.type == mt5.POSITION_TYPE_BUY else "buy"
        self._send({"action": mt5.TRADE_ACTION_DEAL, "position": ticket, "symbol": p.symbol,
                    "volume": round(vol, 2), "type": mt5.ORDER_TYPE_SELL if side == "sell" else mt5.ORDER_TYPE_BUY,
                    "price": self.price(p.symbol, side), "deviation": self.deviation, "magic": self.magic,
                    "type_filling": self._filling(p.symbol), "comment": "tg-copier close"})
        return True

    def cancel_pending(self, ref) -> bool:
        if not mt5.orders_get(ticket=ref):
            return False
        self._send({"action": mt5.TRADE_ACTION_REMOVE, "order": ref})
        return True

    def is_pending(self, ref) -> bool:
        return bool(mt5.orders_get(ticket=ref))
