"""Runs one broker connection inside its own process.

The MetaTrader5 Python package can only talk to ONE terminal per process, so to copy into several
MT5 accounts at once each account gets its own worker process (and its own terminal install).
TradeLocker and paper accounts use the same mechanism for consistency and crash isolation."""
from __future__ import annotations

_broker = None


def make_broker(platform: str, cfg: dict):
    p = platform.lower()
    if p == "mt5":
        from brokers.mt5_broker import MT5Broker
        return MT5Broker(cfg)
    if p == "tradelocker":
        from brokers.tradelocker_broker import TradeLockerBroker
        return TradeLockerBroker(cfg)
    if p == "paper":
        from brokers.paper_broker import PaperBroker
        return PaperBroker(cfg)
    raise ValueError(f"unknown platform {platform}")


def init(platform: str, cfg: dict, root: str):
    import sys
    if root not in sys.path:
        sys.path.insert(0, root)
    global _broker
    _broker = make_broker(platform, cfg)


def call(method: str, *args, **kw):
    return getattr(_broker, method)(*args, **kw)
