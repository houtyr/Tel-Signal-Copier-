"""TradeLocker adapter, built on the official `tradelocker` Python package (pip install tradelocker).
Works on Windows, Mac and Linux. Login is the same email / password / server you use on the
TradeLocker web terminal for your prop account."""
from __future__ import annotations

import logging
import time
from typing import Optional

from .base import AccountInfo, Broker, Position, SymbolSpec

try:
    from tradelocker import TLAPI
except ImportError:
    TLAPI = None

log = logging.getLogger("tradelocker")

# Contract size per 1.0 lot when the instrument details don't report one. Verify against your
# prop firm's contract specs and override in config.yaml -> contract_sizes.
DEFAULT_CONTRACT_SIZES = {"XAUUSD": 100, "XAGUSD": 5000, "US30": 1, "NAS100": 1, "SPX500": 1,
                          "GER40": 1, "BTCUSD": 1, "ETHUSD": 1, "USOIL": 1000}

# TradeLocker answers 429 "Too Many Requests" when an account is polled too often. Reads are cached for
# REFRESH_SEC so one poll cycle shares a single request per endpoint; after a 429 we stop reading for
# QUIET_SEC and serve the last good data instead.
REFRESH_SEC = 2.0
QUIET_SEC = 10.0
WRITE_RETRY_WAITS = (2, 5)


def is_rate_limit(err: Exception) -> bool:
    s = str(err)
    return "429" in s or "Too Many Requests" in s


class TradeLockerBroker(Broker):
    def __init__(self, cfg: dict):
        self.name = cfg["name"]
        self.cfg = cfg
        self.tl = None
        self.currency = "USD"
        self.strategy_id = str(cfg.get("strategy_id", "tg-copier"))[:31]
        self._ids: dict[str, int] = {}
        self._names: dict[int, str] = {}
        self._cache: dict[str, tuple[float, object]] = {}
        self._quiet_until = 0.0

    # ---------- request pacing ----------
    def _rate_limited(self) -> RuntimeError:
        self._quiet_until = time.time() + QUIET_SEC
        log.warning("[%s] TradeLocker rate limit hit - pausing reads for %.0fs", self.name, QUIET_SEC)
        return RuntimeError(f"TradeLocker rate limit (429 Too Many Requests) - slowing down for {QUIET_SEC:.0f}s")

    def _read(self, key: str, fn, force: bool = False):
        """Cached read. Serves the last good value while TradeLocker is rate-limiting us."""
        now = time.time()
        hit = self._cache.get(key)
        if hit and (now < self._quiet_until or now - hit[0] < (0.5 if force else REFRESH_SEC)):
            return hit[1]
        if now < self._quiet_until:
            raise RuntimeError("TradeLocker rate limit (429 Too Many Requests) - waiting before the next request")
        try:
            val = fn()
        except Exception as e:
            if not is_rate_limit(e):
                raise
            err = self._rate_limited()
            if hit:
                return hit[1]
            raise err from None
        self._cache[key] = (time.time(), val)
        return val

    def _write(self, fn):
        """Order changes are retried after a 429 (the request was refused, not executed)."""
        for wait in WRITE_RETRY_WAITS + (None,):
            try:
                out = fn()
                break
            except Exception as e:
                if not is_rate_limit(e):
                    raise
                if wait is None:
                    raise self._rate_limited() from None
                log.warning("[%s] TradeLocker rate limit on an order request - retrying in %ss", self.name, wait)
                time.sleep(wait)
        self._cache = {k: (0.0, v) for k, (_, v) in self._cache.items()}   # expire reads, keep them as fallback
        return out

    def _orders(self, history: bool = False, force: bool = False):
        return self._read("history" if history else "orders", lambda: self.tl.get_all_orders(history=history), force)

    def connect(self) -> None:
        if TLAPI is None:
            raise RuntimeError("tradelocker package not installed: pip install tradelocker")
        env = self.cfg.get("environment", "https://demo.tradelocker.com")
        login = dict(environment=env, username=self.cfg["email"], password=self.cfg["password"],
                     server=self.cfg["server"], log_level="warning")
        num = int(self.cfg["acc_num"]) if self.cfg.get("acc_num") else 0
        try:
            self.tl = TLAPI(**login, **({"acc_num": num} if num else {}))
        except ValueError as e:
            # People usually paste the account ID shown in the TradeLocker terminal (e.g. 2574741), not the
            # short acc_num (e.g. 1) - accept either
            if not num or "not found in all_accounts" not in str(e):
                raise
            try:
                self.tl = TLAPI(**login, account_id=num)
            except ValueError:
                raise RuntimeError(f"[{self.name}] account {num} isn't on this TradeLocker login - use the "
                                   f"account ID or acc number from this list:\n{e}") from None
        try:
            accts = self.tl.get_all_accounts()
            row = accts[accts["id"] == self.tl.account_id]
            if not row.empty:
                self.currency = str(row["currency"].iloc[0])
        except Exception:
            pass

    def account(self) -> AccountInfo:
        s = self._read("state", self.tl.get_account_state)
        return AccountInfo(float(s["balance"]), float(s.get("projectedBalance", s["balance"])), self.currency)

    # ---------- symbols ----------
    def _id(self, symbol: str) -> int:
        if symbol not in self._ids:
            iid = self.tl.get_instrument_id_from_symbol_name(symbol)
            self._ids[symbol] = iid
            self._names[iid] = symbol
        return self._ids[symbol]

    def spec(self, symbol: str) -> SymbolSpec:
        d = self.tl.get_instrument_details(self._id(symbol))
        overrides = self.cfg.get("contract_sizes", {})
        base = symbol.upper()[:6]
        contract = float(overrides.get(symbol) or overrides.get(base) or d.get("lotSize") or d.get("contractSize")
                         or DEFAULT_CONTRACT_SIZES.get(base) or 100000)
        vmin = float(d.get("minOrderSize") or d.get("lotStep") or 0.01)
        vstep = float(d.get("lotStep") or 0.01)
        vmax = float(d.get("maxOrderSize") or 100)
        tick = float(d.get("tickSize") or 0.00001) if not isinstance(d.get("tickSize"), list) else 0.00001
        digits = max(0, len(f"{tick:.10f}".rstrip("0").split(".")[1]))

        # Money value of a 1.0 price move per lot, in account currency
        value = contract
        quote, basec = base[3:6], base[0:3]
        if len(base) == 6 and quote != self.currency:
            px = self.price(symbol, "buy")
            if basec == self.currency:
                value = contract / px          # e.g. USDJPY on a USD account
            else:
                conv = self._convert(quote)
                if conv is None:
                    raise ValueError(f"[{self.name}] can't convert {quote} to {self.currency} for {symbol}; "
                                     f"use fixed_lot for this symbol")
                value = contract * conv
        return SymbolSpec(symbol, vmin, vmax, vstep, value, digits)

    def _convert(self, ccy: str) -> Optional[float]:
        """Return how many account-currency units 1 unit of `ccy` is worth."""
        for sym, invert in ((f"{ccy}{self.currency}", False), (f"{self.currency}{ccy}", True)):
            for cand in (sym, sym + self.cfg.get("symbol_suffix", "")):
                try:
                    px = self.tl.get_latest_asking_price(self._id(cand))
                    return 1 / px if invert else px
                except Exception:
                    continue
        return None

    def price(self, symbol: str, side: str) -> float:
        iid = self._id(symbol)
        return self.tl.get_latest_asking_price(iid) if side == "buy" else self.tl.get_latest_bid_price(iid)

    # ---------- trading ----------
    def open(self, symbol, side, volume, order_type, entry, sl, tp, comment) -> int:
        kw = dict(instrument_id=self._id(symbol), quantity=float(volume), side=side, type_=order_type,
                  strategy_id=self.strategy_id)
        if order_type == "limit":
            kw["price"] = entry
        elif order_type == "stop":
            kw["stop_price"] = entry
        if order_type != "market":
            kw["validity"] = "GTC"
        if sl:
            kw.update(stop_loss=sl, stop_loss_type="absolute")
        if tp:
            kw.update(take_profit=tp, take_profit_type="absolute")
        order_id = self._write(lambda: self.tl.create_order(**kw))
        if not order_id:
            raise RuntimeError(f"[{self.name}] create_order rejected for {symbol} {side} {volume}")
        return int(order_id)

    def _positions_df(self, force: bool = False):
        return self._read("positions", self.tl.get_all_positions, force)

    def positions(self) -> list[Position]:
        df = self._positions_df()
        out = []
        if df is None or df.empty:
            return out
        for _, r in df.iterrows():
            if "strategyId" in df.columns and str(r.get("strategyId") or "") not in ("", self.strategy_id):
                continue
            iid = int(r["tradableInstrumentId"])
            out.append(Position(int(r["id"]), self._names.get(iid, str(iid)), str(r["side"]),
                                float(r["qty"]), float(r["avgPrice"]), None, None,
                                float(r.get("unrealizedPl") or 0.0)))
        return out

    def all_positions(self) -> list[Position]:
        """All positions, with SL/TP read from their attached stop/limit orders."""
        df = self._positions_df()
        if df is None or df.empty:
            return []
        prices = {}
        try:
            orders = self._orders()
            for _, o in orders.iterrows():
                px = o.get("stopPrice") if str(o.get("type")) == "stop" else o.get("price")
                if px == px and px:  # not NaN / 0
                    prices[int(o["id"])] = float(px)
        except Exception as e:
            log.info("orders lookup failed: %s", e)
        out = []
        for _, r in df.iterrows():
            iid = int(r["tradableInstrumentId"])
            if iid not in self._names:
                try:
                    self._names[iid] = self.tl.get_symbol_name_from_instrument_id(iid)
                    self._ids[self._names[iid]] = iid
                except Exception:
                    self._names[iid] = str(iid)
            sl_id, tp_id = r.get("stopLossId"), r.get("takeProfitId")
            sl = prices.get(int(sl_id)) if sl_id == sl_id and sl_id else None
            tp = prices.get(int(tp_id)) if tp_id == tp_id and tp_id else None
            out.append(Position(int(r["id"]), self._names[iid], str(r["side"]), float(r["qty"]), float(r["avgPrice"]),
                                sl, tp, float(r.get("unrealizedPl") or 0.0)))
        return out

    def resolve(self, ref: int) -> Optional[int]:
        """`ref` is an order id. Look up the position it opened (retries briefly; fills are async)."""
        if self.is_pending(ref):
            return None
        for attempt in range(3):
            try:
                hist = self._orders(history=True, force=attempt > 0)
                match = hist[hist["id"] == int(ref)] if hist is not None and not hist.empty else []
                pid = int(match["positionId"].iloc[0]) if len(match) else None
            except Exception as e:
                if is_rate_limit(e):
                    raise
                pid = None
            if pid:
                df = self._positions_df(force=attempt > 0)
                if df is not None and not df.empty and int(pid) in set(df["id"].astype(int)):
                    return int(pid)
                return None  # opened but already closed
            time.sleep(0.5)
        return None

    def leg_states(self, refs: list[int]) -> dict:
        """Batched is_pending/resolve: one read each of pending orders, order history and positions."""
        pend = self._orders()
        pend_ids = set(pend["id"].astype(int)) if pend is not None and not pend.empty else set()
        out = {r: "pending" for r in refs if int(r) in pend_ids}
        todo = [r for r in refs if r not in out]
        if todo:
            hist = self._orders(history=True)
            df = self._positions_df()
            open_ids = set(df["id"].astype(int)) if df is not None and not df.empty else set()
            pid_of = {}
            if hist is not None and not hist.empty:
                for oid, pid in zip(hist["id"].astype(int), hist["positionId"]):
                    if pid == pid and pid:  # not NaN / 0
                        pid_of[int(oid)] = int(pid)
            for r in todo:
                pid = pid_of.get(int(r))
                out[r] = pid if pid in open_ids else None
        return out

    def modify(self, ticket, sl=None, tp=None) -> bool:
        params = {}
        if sl is not None:
            params.update(stopLoss=sl, stopLossType="absolute")
        if tp is not None:
            params.update(takeProfit=tp, takeProfitType="absolute")
        return bool(params) and self._write(lambda: self.tl.modify_position(int(ticket), params))

    def close(self, ticket, volume=None) -> bool:
        return self._write(lambda: self.tl.close_position(position_id=int(ticket), close_quantity=float(volume or 0)))

    def cancel_pending(self, ref) -> bool:
        try:
            return self._write(lambda: self.tl.delete_order(int(ref)))
        except Exception as e:
            if is_rate_limit(e):
                raise
            log.info("cancel %s: %s", ref, e)
            return False

    def is_pending(self, ref) -> bool:
        try:
            df = self._orders()
        except Exception as e:
            if is_rate_limit(e):
                raise
            return False
        return df is not None and not df.empty and int(ref) in set(df["id"].astype(int))
