"""Account-to-account trade mirroring (any direction: MT5 <-> TradeLocker <-> Paper).

A follower account sets `mirror_from` to a source account. Every `mirror_poll_sec` the engine reads the
source's open positions and repeats on each follower:
  new position     -> open the same direction, size scaled per the follower's sizing mode
  volume reduced   -> close the same fraction on the follower (partial close)
  SL / TP changed  -> modify the follower (if mirror_sl_tp is on)
  position gone    -> close the follower's copy
Each follower still goes through its own prop guard (daily loss, drawdown, max positions)."""
from __future__ import annotations

import time

from risk import floor_step, size_position

SIZING_LABEL = {"equity_ratio": "scaled to account size", "multiplier": "lot multiplier",
                "fixed": "fixed lots", "risk": "% risk using the source SL", "same": "same exposure"}


class MirrorMixin:
    """Mixed into Engine. Uses self.settings, self.accounts, self.state, self.store, self.bus, self.note."""

    # ---------- loop ----------
    async def _mirror_loop(self):
        import asyncio
        while True:
            await asyncio.sleep(float(self.settings.get("mirror_poll_sec", 1)))
            groups: dict[str, list[str]] = {}
            for c in self.settings["accounts"]:
                if c.get("mirror_from") and c.get("enabled", True) and c.get("mirror_enabled", True):
                    groups.setdefault(c["mirror_from"], []).append(c["name"])
            for src in list(self.state["mirror"]):
                if src not in groups:
                    self.state["mirror"].pop(src)   # nobody follows it any more
            for src, fols in groups.items():
                try:
                    await self._mirror_source(src, fols)
                except Exception as e:
                    if src in self.accounts and self.rate_limit_pause(self.accounts[src], e):
                        continue
                    self.note("error", src, f"mirror: {e}")

    def mirror_groups(self) -> list[dict]:
        out = {}
        for c in self.settings["accounts"]:
            if c.get("mirror_from"):
                g = out.setdefault(c["mirror_from"], {"source": c["mirror_from"], "followers": []})
                g["followers"].append({"name": c["name"], "sizing": c.get("mirror_sizing", "equity_ratio"),
                                       "value": c.get("mirror_value"), "enabled": c.get("mirror_enabled", True),
                                       "sl_tp": c.get("mirror_sl_tp", True)})
        for src, g in out.items():
            pos = self.state["mirror"].get(src, {}).get("pos", {})
            g["tracked"] = len(pos)
            for f in g["followers"]:     # source trades this follower currently holds a copy of
                f["copies"] = sum(1 for r in pos.values() if r["f"].get(f["name"]))
        return list(out.values())

    # ---------- one source ----------
    async def _mirror_source(self, src: str, fols: list[str]):
        acc = self.accounts.get(src)
        if not acc or acc.status != "connected" or time.time() < acc.paused_until:
            return
        now = time.time()
        cur = {str(p.ticket): p for p in await acc.call("all_positions")}
        first_time = src not in self.state["mirror"]
        ms = self.state["mirror"].setdefault(src, {"pos": {}})
        changed = False

        # new + changed positions
        for t, p in cur.items():
            rec = ms["pos"].get(t)
            if rec is None:
                rec = ms["pos"][t] = {"symbol": p.symbol, "side": p.side, "volume": p.volume, "open_price": p.open_price,
                                      "sl": p.sl, "tp": p.tp, "first_seen": 0 if first_time else now, "f": {}}
                changed = True
            todo = [f for f in fols if f not in rec["f"]]
            if todo:
                changed |= await self._mirror_open_all(src, t, rec, todo)
            if p.volume < rec["volume"] - 1e-9:
                await self._mirror_partial(src, t, rec, p.volume)
                rec["volume"] = p.volume
                changed = True
            if (p.sl or None) != (rec["sl"] or None) or (p.tp or None) != (rec["tp"] or None):
                await self._mirror_modify(src, t, rec, p.sl, p.tp)
                rec["sl"], rec["tp"] = p.sl, p.tp
                changed = True

        # closed on the source
        for t in [t for t in ms["pos"] if t not in cur]:
            await self._mirror_close(src, t, ms["pos"].pop(t))
            changed = True
        if changed:
            self.save_state()

    # ---------- feed helper ----------
    def _mirror_msg(self, src: str, text: str, parsed: dict):
        from engine import current_msg, current_notes
        mid = self.store.add_message(-1, f"Mirror from {src}", 0, None, text, "mirror", parsed)
        notes: list = []
        current_msg.set(mid)
        current_notes.set(notes)
        return mid, notes

    def _mirror_done(self, mid, notes):
        status = "error" if any(n["level"] == "error" for n in notes) else "done"
        self.store.finish_message(mid, notes, status)
        self.bus.emit("message", self.store.message(mid))
        from engine import current_msg, current_notes
        current_msg.set(None)
        current_notes.set(None)

    def _canonical(self, src: str, symbol: str) -> str:
        """Source broker's symbol name -> plain symbol (undo its mapping / suffix)."""
        c = self.settings.account(src) or {}
        inv = {v: k for k, v in (c.get("symbol_map") or {}).items()}
        if symbol in inv:
            return inv[symbol]
        suf = c.get("symbol_suffix") or ""
        return symbol[: -len(suf)] if suf and symbol.endswith(suf) else symbol

    # ---------- actions ----------
    async def _mirror_open_all(self, src: str, t: str, rec: dict, fols: list[str]) -> bool:
        """Copy a new source position to followers. One feed entry per source trade."""
        act = []
        for fol in fols:
            fc = self.settings.account(fol) or {}
            pre_existing = rec["first_seen"] == 0 or rec["first_seen"] < float(fc.get("mirror_since") or 0)
            if pre_existing and not fc.get("mirror_existing"):
                rec["f"][fol] = []          # was already open before this link was set up - leave it
                continue
            fa = self.accounts.get(fol)
            if fa and fa.status == "connected":
                act.append((fol, pre_existing))
            # else: retry next poll when it connects (subject to the delay limit)
        if not act:
            return True
        mid, notes = self._mirror_msg(src, f"{rec['side'].upper()} {rec['symbol']} {rec['volume']:.2f} lots @ {rec['open_price']:.5g}"
                                           f"   SL {rec['sl'] or '-'}   TP {rec['tp'] or '-'}",
                                      {"event": "open", **{k: rec[k] for k in ("symbol", "side", "volume", "sl", "tp")}})
        for fol, pre_existing in act:
            rec["f"][fol] = []
            try:
                await self._mirror_open_one(src, t, rec, fol, pre_existing)
            except Exception as e:
                self.note("error", fol, f"mirror open failed: {e}")
        self._mirror_done(mid, notes)
        return True

    async def _mirror_open_one(self, src: str, t: str, rec: dict, fol: str, pre_existing: bool):
        fa = self.accounts[fol]
        fc = self.settings.account_runtime_cfg(fol)
        age = time.time() - rec["first_seen"]
        max_delay = float(self.settings.get("mirror_max_delay_sec", 30))
        if not pre_existing and age > max_delay:
            self.note("warn", fol, f"skipped - {age:.0f}s after the source trade (max {max_delay:.0f}s), price may have moved")
            return
        if not self.settings["copying_enabled"]:
            self.note("warn", fol, "skipped - copying is paused"); return
        if fa.halted:
            self.note("warn", fol, f"skipped - halted ({fa.halted})"); return
        canon = self._canonical(src, rec["symbol"])
        if not fa.allowed(canon):
            self.note("info", fol, f"skipped - {canon} not allowed on this account"); return
        info = await fa.call("account")
        start = await self.day_start_equity(fa)
        reason = fa.guard.check_new_trade(start, info.equity, len(await fa.call("positions")),
                                          self.state["daily"][fol]["signals"])
        if reason:
            self.note("warn", fol, f"skipped - {reason}"); return
        sym = fa.broker_symbol(canon)
        vol = await self._mirror_volume(src, fol, rec, canon, sym, info.equity)
        if vol <= 0:
            self.note("warn", fol, "skipped - size rounds below the minimum lot"); return
        copy_levels = fc.get("mirror_sl_tp", True)
        sl, tp = (rec["sl"], rec["tp"]) if copy_levels else (None, None)
        if fc.get("require_sl", True) and not sl:
            self.note("warn", fol, "note: source trade has no stop loss - copy opened without one")
        if self.settings["dry_run"]:
            self.note("ok", fol, f"DRY RUN - would have opened {rec['side'].upper()} {sym} {vol:.2f} lots"); return
        ref = await fa.call("open", sym, rec["side"], vol, "market", None, sl, tp, f"mirror {src[:12]}")
        rec["f"][fol] = [ref]
        self.state["daily"][fol]["signals"] += 1
        self.store.add_trade(fol, f"mirror:{src}:{t}", "open", sym, rec["side"], vol, ref, f"mirror of {src} #{t}")
        self.note("ok", fol, f"opened {rec['side'].upper()} {sym} {vol:.2f} lots "
                             f"({SIZING_LABEL.get(fc.get('mirror_sizing') or 'equity_ratio', '')})")

    async def _mirror_volume(self, src, fol, rec, canon, fsym, fol_equity) -> float:
        sa, fa = self.accounts[src], self.accounts[fol]
        fc = self.settings.account_runtime_cfg(fol)
        mode = fc.get("mirror_sizing") or "equity_ratio"
        val = float(fc.get("mirror_value") or (1.0 if mode != "risk" else fc.get("risk_pct", 0.5)))
        sspec = await sa.call("spec", rec["symbol"])
        fspec = await fa.call("spec", fsym)
        # Contract sizes can differ between brokers (e.g. US30 = 1 vs 10 per lot); match money exposure, not lot count.
        exposure = sspec.value_per_price_unit / fspec.value_per_price_unit if fspec.value_per_price_unit else 1.0
        if mode == "fixed":
            vol = val
        elif mode == "same":
            vol = rec["volume"] * exposure
        elif mode == "multiplier":
            vol = rec["volume"] * exposure * val
        elif mode == "risk" and rec["sl"]:
            risk_money = fa.guard.initial * val / 100
            return size_position(risk_money, await fa.call("price", fsym, rec["side"]), rec["sl"], fspec)
        else:  # equity_ratio (also the fallback for "risk" when the source has no SL)
            src_eq = (sa.info.equity if sa.info else 0) or 1
            vol = rec["volume"] * exposure * (fol_equity / src_eq) * (val if mode == "equity_ratio" else 1)
        vol = min(floor_step(vol, fspec.volume_step), fspec.volume_max)
        return vol if vol >= fspec.volume_min else 0.0

    async def _live_copies(self, fol: str, rec: dict):
        fa = self.accounts.get(fol)
        if not fa or fa.status != "connected":
            return fa, []
        return fa, [x for x in [await fa.call("resolve", r) for r in rec["f"].get(fol, [])] if x]

    async def _mirror_partial(self, src, t, rec, new_vol):
        frac = 1 - new_vol / rec["volume"]
        followers = [f for f, refs in rec["f"].items() if refs]
        if not followers:
            return
        mid, notes = self._mirror_msg(src, f"Partial close {rec['symbol']}: {rec['volume']:.2f} → {new_vol:.2f} lots ({frac:.0%})",
                                      {"event": "partial", "symbol": rec["symbol"], "fraction": frac})
        for fol in followers:
            try:
                fa, live = await self._live_copies(fol, rec)
                pos = {p.ticket: p for p in await fa.call("positions")} if live else {}
                for x in live:
                    if self.settings["dry_run"]:
                        continue
                    await fa.call("close", x, pos[x].volume * frac if x in pos else None)
                self.note("ok" if live else "info", fol, f"closed {frac:.0%}" if live else "copy already closed")
            except Exception as e:
                self.note("error", fol, f"partial close failed: {e}")
        self._mirror_done(mid, notes)

    async def _mirror_modify(self, src, t, rec, sl, tp):
        followers = [f for f, refs in rec["f"].items() if refs and (self.settings.account(f) or {}).get("mirror_sl_tp", True)]
        if not followers:
            return
        mid, notes = self._mirror_msg(src, f"{rec['symbol']} SL {rec['sl'] or '-'} → {sl or '-'}   TP {rec['tp'] or '-'} → {tp or '-'}",
                                      {"event": "modify", "symbol": rec["symbol"], "sl": sl, "tp": tp})
        for fol in followers:
            try:
                fa, live = await self._live_copies(fol, rec)
                for x in live:
                    if not self.settings["dry_run"]:
                        await fa.call("modify", x, sl=sl if sl != rec["sl"] else None, tp=tp if tp != rec["tp"] else None)
                self.note("ok" if live else "info", fol, "SL/TP updated" if live else "copy already closed")
            except Exception as e:
                self.note("error", fol, f"modify failed: {e}")
        self._mirror_done(mid, notes)

    async def _mirror_close(self, src, t, rec):
        followers = [f for f, refs in rec["f"].items() if refs]
        if not followers:
            return
        mid, notes = self._mirror_msg(src, f"Closed {rec['side'].upper()} {rec['symbol']} on {src}",
                                      {"event": "close", "symbol": rec["symbol"]})
        for fol in followers:
            try:
                fa, live = await self._live_copies(fol, rec)
                for x in live:
                    if not self.settings["dry_run"]:
                        await fa.call("close", x)
                    self.store.add_trade(fol, f"mirror:{src}:{t}", "close", rec["symbol"], rec["side"], None, x, f"source {src} closed")
                self.note("ok" if live else "info", fol, f"closed {len(live)} copy" if live else "copy was already closed")
            except Exception as e:
                self.note("error", fol, f"close failed: {e}")
        self._mirror_done(mid, notes)
