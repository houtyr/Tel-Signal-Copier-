"""App settings, saved to settings.json next to the app. Edited from the UI.

Passwords are stored in this file on your computer. Any value can instead be written as
${ENV_VAR_NAME} to read it from an environment variable."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

SECRET_KEYS = {"password", "api_hash"}
MASK = "••••••••"

DEFAULTS = {
    "dry_run": True,
    "copying_enabled": True,
    "max_signal_age_sec": 60,
    "unlinked_commands": "only_if_one_open",
    "mirror_poll_sec": 1,
    "mirror_max_delay_sec": 30,
    "poll_sec": 3,
    "day_reset": {"timezone": "America/New_York", "hour": 17},
    "symbol_aliases": {},
    "telegram": {"api_id": "", "api_hash": "", "phone": "", "session": "copier"},
    "channels": [],          # [{"id": -100123, "title": "Gold VIP", "enabled": true}]
    "risk": {
        "risk_pct": 0.5, "risk_base": "initial", "risk_multiplier": 1.0, "fixed_lot": None,
        "require_sl": True, "default_sl_distance": {}, "split_tps": True, "max_tps": 3,
        "use_final_tp_only": False, "breakeven_after_tp1": True, "breakeven_offset": 0.0,
        "max_entry_slip_of_sl": 0.3, "if_price_moved": "limit",
        "zone_entry": "layers", "tp_hit_action": "breakeven", "add_on_entries": "half",
        # Trade management - distances are multiples of R (the entry-to-SL distance), so they suit every symbol
        "be_trigger_r": 0.3, "be_lock_r": 0.05, "trail_tp_ladder": True, "trail_start_r": 1.0, "trail_distance_r": 0.5,
        "min_tp_distance_r": 0.1, "runner_tp": "last", "runner_max_r": 3.0,
        "pending_expiry_min": 90, "cancel_pending_at_tp1": True, "cancel_pending_on_new_signal": True,
        "max_signals_per_symbol": 2,
        "daily_stop_pct": 4.0, "daily_basis": "initial", "total_stop_pct": 9.0, "flatten_on_stop": True,
        "max_open_positions": 6, "max_signals_per_day": 8, "stop_at_profit_target_pct": None,
        "allowed_symbols": [], "blocked_symbols": [],
    },
    "accounts": [],
    "ai": {"mode": "off", "provider": "anthropic", "model": "claude-haiku-5-5", "api_key": "", "base_url": "",
           "min_confidence": 0.8, "auto_manage": True, "timeout_sec": 20},
}

# Fields shown in the account editor, per platform
ACCOUNT_FIELDS = {
    "mt5": ["login", "password", "server", "terminal_path", "symbol_suffix", "magic", "max_slippage_points"],
    "tradelocker": ["environment", "email", "password", "server", "acc_num", "symbol_suffix"],
    "paper": ["starting_balance"],
}


def _expand(v):
    if isinstance(v, str):
        return os.path.expandvars(v)
    if isinstance(v, dict):
        return {k: _expand(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_expand(x) for x in v]
    return v


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k not in ("symbol_aliases", "symbol_map",
                                                                          "contract_sizes", "default_sl_distance"):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Settings:
    def __init__(self, path: Path):
        self.path = path
        self.data = copy.deepcopy(DEFAULTS)
        if path.exists():
            self.data = _merge(DEFAULTS, json.loads(path.read_text(encoding="utf-8")))

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    # ---- accessors ----
    def __getitem__(self, k):
        return self.data[k]

    def get(self, k, d=None):
        return self.data.get(k, d)

    def account(self, name: str) -> dict | None:
        return next((a for a in self.data["accounts"] if a["name"] == name), None)

    def account_runtime_cfg(self, name: str) -> dict:
        """Account config merged over the global risk defaults, with ${ENV} expanded."""
        a = self.account(name)
        return _expand({**self.data["risk"], **{k: v for k, v in a.items() if v not in (None, "")}})

    def telegram(self) -> dict:
        return _expand(self.data["telegram"])

    def ai(self) -> dict:
        return _expand(self.data["ai"])

    def set_ai(self, patch: dict):
        for k in ("mode", "provider", "model", "api_key", "base_url", "min_confidence", "auto_manage", "timeout_sec"):
            if k in patch and not (k == "api_key" and patch[k] == MASK):
                self.data["ai"][k] = patch[k]
        self.save()

    # ---- updates from the UI ----
    def update_global(self, patch: dict):
        for k in ("dry_run", "copying_enabled", "max_signal_age_sec", "unlinked_commands", "poll_sec",
                  "day_reset", "symbol_aliases", "risk", "mirror_poll_sec", "mirror_max_delay_sec"):
            if k in patch:
                self.data[k] = _merge(self.data[k], patch[k]) if isinstance(patch[k], dict) and k == "risk" else patch[k]
        self.save()

    def upsert_account(self, acc: dict, original_name: str | None = None) -> dict:
        import time
        old = self.account(original_name or acc["name"])
        acc = {k: v for k, v in acc.items()}
        # ---- account-to-account mirroring checks
        src = acc.get("mirror_from") or None
        acc["mirror_from"] = src
        if src:
            if src == acc["name"]:
                raise ValueError("an account can't mirror itself")
            if not self.account(src):
                raise ValueError(f"source account {src} doesn't exist")
            seen, cur = {acc["name"]}, src
            while cur:                                   # walk up the chain to catch A -> B -> A loops
                if cur in seen:
                    raise ValueError("that would create a copy loop between accounts")
                seen.add(cur)
                cur = (self.account(cur) or {}).get("mirror_from")
        if src != (old or {}).get("mirror_from"):
            acc["mirror_since"] = time.time()           # only trades opened after this moment are mirrored
        for k in SECRET_KEYS:
            if acc.get(k) in (MASK, None, "") and old:
                acc[k] = old.get(k, "")
        if old:
            idx = self.data["accounts"].index(old)
            self.data["accounts"][idx] = {**old, **acc}
            if original_name and original_name != acc["name"]:
                for a in self.data["accounts"]:
                    if a.get("mirror_from") == original_name:
                        a["mirror_from"] = acc["name"]
        else:
            if self.account(acc["name"]):
                raise ValueError("an account with that name already exists")
            self.data["accounts"].append(acc)
        self.save()
        return self.account(acc["name"])

    def delete_account(self, name: str):
        self.data["accounts"] = [a for a in self.data["accounts"] if a["name"] != name]
        for a in self.data["accounts"]:
            if a.get("mirror_from") == name:
                a["mirror_from"] = None
        self.save()

    def set_telegram(self, patch: dict):
        for k in ("api_id", "api_hash", "phone"):
            if k in patch and patch[k] not in (MASK, None):
                self.data["telegram"][k] = str(patch[k]).strip()
        self.save()

    def set_channels(self, channels: list[dict]):
        self.data["channels"] = [{"id": int(c["id"]), "title": c.get("title", ""), "enabled": bool(c.get("enabled", True))}
                                 for c in channels]
        self.save()

    def public(self) -> dict:
        """Settings safe to send to the browser (secrets masked)."""
        d = copy.deepcopy(self.data)
        for a in d["accounts"]:
            for k in SECRET_KEYS:
                if a.get(k):
                    a[k] = MASK
        if d["telegram"].get("api_hash"):
            d["telegram"]["api_hash"] = MASK
        if d["ai"].get("api_key"):
            d["ai"]["api_key"] = MASK
        d["account_fields"] = ACCOUNT_FIELDS
        return d
