from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class AccountInfo:
    balance: float
    equity: float
    currency: str


@dataclass
class SymbolSpec:
    name: str                 # broker symbol name (after mapping/suffix)
    volume_min: float
    volume_max: float
    volume_step: float
    value_per_price_unit: float   # account-currency P/L for a 1.0 price move on 1.0 lot
    digits: int


@dataclass
class Position:
    ticket: int
    symbol: str
    side: str            # buy | sell
    volume: float
    open_price: float
    sl: Optional[float]
    tp: Optional[float]
    profit: float = 0.0          # floating P/L in account currency
    price: float = 0.0           # current price


class Broker(ABC):
    """Every broker adapter implements this interface. All methods are blocking;
    engine.py runs each account in its own worker process (so several MT5 terminals can run side by side)."""

    name: str

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def account(self) -> AccountInfo: ...

    @abstractmethod
    def spec(self, symbol: str) -> SymbolSpec: ...

    @abstractmethod
    def price(self, symbol: str, side: str) -> float:
        """Price you would get filled at now (ask for buy, bid for sell)."""

    @abstractmethod
    def open(self, symbol: str, side: str, volume: float, order_type: str,
             entry: Optional[float], sl: Optional[float], tp: Optional[float], comment: str) -> int:
        """Place an order. Returns an id that positions()/modify()/close() understand."""

    @abstractmethod
    def positions(self) -> list[Position]:
        """Open positions placed by this copier."""

    def all_positions(self) -> list[Position]:
        """Every open position on the account, including manual trades and other EAs.
        Used when this account is the SOURCE of account-to-account mirroring."""
        return self.positions()

    @abstractmethod
    def resolve(self, ref: int) -> Optional[int]:
        """Map the id returned by open() to a live position ticket (None if not filled / closed)."""

    @abstractmethod
    def modify(self, ticket: int, sl: Optional[float] = None, tp: Optional[float] = None) -> bool: ...

    @abstractmethod
    def close(self, ticket: int, volume: Optional[float] = None) -> bool: ...

    @abstractmethod
    def cancel_pending(self, ref: int) -> bool: ...

    @abstractmethod
    def is_pending(self, ref: int) -> bool:
        """True if `ref` is a pending (unfilled) limit/stop order."""
