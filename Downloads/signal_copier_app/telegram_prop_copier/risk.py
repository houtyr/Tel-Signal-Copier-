"""Position sizing and prop-firm guardrails."""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from brokers.base import SymbolSpec


def floor_step(x: float, step: float) -> float:
    return round(math.floor(x / step + 1e-9) * step, 8)


def size_position(risk_money: float, entry: float, sl: float, spec: SymbolSpec,
                  max_overshoot: float = 1.25) -> float:
    """Lots so that hitting SL loses ~risk_money. Returns 0 if even the minimum lot is too risky."""
    dist = abs(entry - sl)
    if dist <= 0 or spec.value_per_price_unit <= 0:
        return 0.0
    loss_per_lot = dist * spec.value_per_price_unit
    lots = floor_step(risk_money / loss_per_lot, spec.volume_step)
    if lots < spec.volume_min:
        # Only allow the minimum lot if it doesn't overshoot the risk budget by much
        return spec.volume_min if spec.volume_min * loss_per_lot <= risk_money * max_overshoot else 0.0
    return min(lots, spec.volume_max)


def split_volume(total: float, legs: int, spec: SymbolSpec) -> list[float]:
    """Split `total` lots across up to `legs` positions, each a valid lot size."""
    while legs > 1:
        each = floor_step(total / legs, spec.volume_step)
        if each >= spec.volume_min:
            vols = [each] * legs
            vols[-1] = floor_step(total - each * (legs - 1), spec.volume_step)  # leftover to the runner
            return vols
        legs -= 1
    return [total]


def trading_day(now_utc: datetime, tz: str, reset_hour: int) -> str:
    """Prop firms reset the daily loss at a fixed local time (often 5 pm New York).
    Returns a label for the current 'trading day'."""
    local = now_utc.astimezone(ZoneInfo(tz))
    if local.hour >= reset_hour:
        local += timedelta(days=1)
    return local.strftime("%Y-%m-%d")


class PropGuard:
    """Blocks new trades (and optionally flattens) before prop limits are breached."""

    def __init__(self, cfg: dict):
        self.initial = float(cfg["initial_balance"])
        self.daily_stop_pct = float(cfg.get("daily_stop_pct", 4.0))       # stop before the firm's 5%
        self.total_stop_pct = float(cfg.get("total_stop_pct", 9.0))       # stop before the firm's 10%
        self.daily_basis = cfg.get("daily_basis", "initial")              # "initial" | "day_start"
        self.max_open = int(cfg.get("max_open_positions", 6))
        self.max_signals_per_day = int(cfg.get("max_signals_per_day", 8))
        self.profit_target_pct = cfg.get("stop_at_profit_target_pct")     # e.g. 8 for phase 1, None to ignore

    def daily_loss_pct(self, day_start_equity: float, equity: float) -> float:
        base = self.initial if self.daily_basis == "initial" else day_start_equity
        return max(0.0, (day_start_equity - equity) / base * 100)

    def total_dd_pct(self, equity: float) -> float:
        return max(0.0, (self.initial - equity) / self.initial * 100)

    def check_new_trade(self, day_start_equity: float, equity: float, open_positions: int,
                        signals_today: int) -> str | None:
        """Return a reason string if a new trade must be blocked, else None."""
        if self.daily_loss_pct(day_start_equity, equity) >= self.daily_stop_pct:
            return f"daily loss {self.daily_loss_pct(day_start_equity, equity):.2f}% >= {self.daily_stop_pct}%"
        if self.total_dd_pct(equity) >= self.total_stop_pct:
            return f"total drawdown {self.total_dd_pct(equity):.2f}% >= {self.total_stop_pct}%"
        if open_positions >= self.max_open:
            return f"{open_positions} open positions (max {self.max_open})"
        if signals_today >= self.max_signals_per_day:
            return f"{signals_today} signals taken today (max {self.max_signals_per_day})"
        if self.profit_target_pct and (equity - self.initial) / self.initial * 100 >= float(self.profit_target_pct):
            return "profit target reached - not risking the pass"
        return None

    def must_flatten(self, day_start_equity: float, equity: float) -> str | None:
        if self.daily_loss_pct(day_start_equity, equity) >= self.daily_stop_pct:
            return "daily loss stop"
        if self.total_dd_pct(equity) >= self.total_stop_pct:
            return "total drawdown stop"
        return None
