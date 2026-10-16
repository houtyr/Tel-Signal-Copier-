"""AI interpreter for Telegram messages the rule-based parser can't read with confidence.

Works with any cheap model:
  - Claude (Anthropic API)                      provider = "anthropic"
  - Anything OpenAI-compatible                  provider = "openai"
      Google Gemini (free tier available)       base_url = https://generativelanguage.googleapis.com/v1beta/openai/
      Groq (free tier available)                base_url = https://api.groq.com/openai/v1
      Ollama on this PC (free, private)         base_url = http://localhost:11434/v1
      OpenRouter, DeepSeek, Together, ...

The model only ever returns a small JSON object. The engine validates it and decides what to do;
new trades proposed by the AI always wait for your approval in the feed."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import unicodedata

import requests

from signal_parser import Command, Signal

log = logging.getLogger("ai")

ACTIONS = {"close_all", "close_partial", "close_worst", "breakeven", "move_sl", "move_tp", "cancel_pending"}
LEVELS = {"tp1", "tp2", "tp3", "tp4", "tp5", "tp6", "layer_low", "layer_high", "layer1", "layer2"}

SYSTEM_PROMPT = """You read messages from a forex/gold trading signal channel on Telegram and convert them to JSON instructions for a trade copier.

Return ONLY one JSON object, no prose, in exactly this shape:
{"kind": "new_signal" | "add_entries" | "manage" | "info",
 "confidence": 0.0-1.0,
 "reason": "max 15 words",
 "signal": {"symbol": "XAUUSD", "side": "buy" | "sell", "entries": [numbers], "sl": number | null, "tps": [numbers]} | null,
 "actions": [{"type": "close_all" | "close_partial" | "close_worst" | "breakeven" | "move_sl" | "move_tp" | "cancel_pending",
              "fraction": number | null, "price": number | null, "level": "tp1".."tp6" | "layer_low" | "layer_high" | "layer1" | "layer2" | null}]}

Meanings:
- new_signal: a complete new trade with entry and stop loss. "4139//4129" means two entry layers (first is near market).
- add_entries: "add more" / re-entry on the trade being replied to. Put the new entries in signal.entries, symbol may be null.
  If it also updates the stop loss, put the new SL in signal.sl.
- manage: an instruction about the existing trade: close all, close part (fraction 0-1), close only the worst-priced
  entries (close_worst), set breakeven (optionally at an entry layer: "lower part" = layer_low, "higher layer" = layer_high,
  "2nd zone" = layer2), move SL to a price or to a TP level, move TP, cancel pending orders.
- info: anything else - progress updates ("running 60 pips"), "TP1 hit", "BE hit", results, warnings, opinions,
  questions, motivation, or an instruction that is conditional or negated.

Rules:
- Only produce actions the channel is clearly telling members to do NOW.
- Channels make typos: "Breakevn" = breakeven, "Wrost Enteries" = worst entries, "not" is sometimes a typo for "now"
  when the sentence otherwise makes no sense ("Set Breakevn not at higher layer" = set breakeven now at the higher layer).
  Lower your confidence when you rely on a typo guess.
- "Hold X & set breakeven" = breakeven (holding is not an action).
- Never invent prices. Use null when a value is not given.
- confidence = how sure you are that a trader in this channel would read it the same way."""


def _norm(t: str) -> str:
    return unicodedata.normalize("NFKC", t).upper()


INFO_ONLY = re.compile(r"\bRUNNING\b|\bRESULTS?\b|\bSUMMARY\b|\bSTICKER\b|\bPIPS\s+PROFIT\b|^\s*(?:BE|BREAK\s?EVEN)\s+HIT\b|"
                       r"\b(?:WON|WIN)\s+\d", re.M)
ACTION_WORDS = re.compile(r"\b(CLOSE|EXIT|BE|B/E|BREAK|BREAKE\w*|SL|STOP|TP\d?|PARTIALS?|SECURE|RISK|CANCEL|ADD|ENTR\w*|"
                          r"ENTER\w*|LAYERS?|ZONE|LIMITS?|UPDATE|MOVE|SET|BUY|SELL|HALF|HOLD)\b")


class Interpreter:
    def __init__(self, settings):
        self.settings = settings
        self.calls_today = 0
        self.tokens_today = [0, 0]
        self._day = time.strftime("%Y-%m-%d")

    @property
    def cfg(self) -> dict:
        return self.settings.ai()

    def enabled(self) -> bool:
        c = self.cfg
        return c.get("mode", "off") != "off" and bool(c.get("model")) and (bool(c.get("api_key")) or c.get("provider") == "openai"
                                                                           and "localhost" in (c.get("base_url") or ""))

    def should_ask(self, text: str, rules_result, is_reply_to_trade: bool) -> bool:
        if not self.enabled():
            return False
        t = _norm(text)
        if INFO_ONLY.search(t) or len(t.strip()) < 2:
            return False
        if self.cfg.get("mode") == "always":
            return True
        if rules_result is not None:
            return False
        return is_reply_to_trade or bool(ACTION_WORDS.search(t))

    def stats(self) -> dict:
        return {"calls_today": self.calls_today, "input_tokens": self.tokens_today[0], "output_tokens": self.tokens_today[1]}

    # ---------- the call ----------
    async def interpret(self, text: str, context: str) -> dict:
        user = f"{context}\n\nMESSAGE:\n{text.strip()}" if context else f"MESSAGE:\n{text.strip()}"
        raw, usage = await asyncio.to_thread(self._call, user)
        today = time.strftime("%Y-%m-%d")
        if today != self._day:
            self._day, self.calls_today, self.tokens_today = today, 0, [0, 0]
        self.calls_today += 1
        self.tokens_today[0] += usage[0]
        self.tokens_today[1] += usage[1]
        return validate(_extract_json(raw))

    def _call(self, user: str) -> tuple[str, tuple[int, int]]:
        c = self.cfg
        timeout = float(c.get("timeout_sec") or 20)
        if c.get("provider") == "anthropic":
            r = requests.post("https://api.anthropic.com/v1/messages", timeout=timeout,
                              headers={"x-api-key": c["api_key"], "anthropic-version": "2023-06-01", "content-type": "application/json"},
                              json={"model": c["model"], "max_tokens": 400, "temperature": 0,
                                    "system": [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
                                    "messages": [{"role": "user", "content": user}]})
            if r.status_code >= 400:
                raise RuntimeError(f"AI provider error {r.status_code}: {r.text[:200]}")
            d = r.json()
            text = "".join(b.get("text", "") for b in d.get("content", []))
            u = d.get("usage", {})
            return text, (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0), u.get("output_tokens", 0))
        # OpenAI-compatible
        base = (c.get("base_url") or "https://api.openai.com/v1").rstrip("/")
        headers = {"content-type": "application/json"}
        if c.get("api_key"):
            headers["authorization"] = f"Bearer {c['api_key']}"
        body = {"model": c["model"], "temperature": 0, "max_tokens": 400,
                "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}],
                "response_format": {"type": "json_object"}}
        r = requests.post(f"{base}/chat/completions", headers=headers, json=body, timeout=timeout)
        if r.status_code == 400 and "response_format" in r.text:
            body.pop("response_format")            # some providers don't support JSON mode - the prompt is enough
            r = requests.post(f"{base}/chat/completions", headers=headers, json=body, timeout=timeout)
        if r.status_code >= 400:
            raise RuntimeError(f"AI provider error {r.status_code}: {r.text[:200]}")
        d = r.json()
        u = d.get("usage") or {}
        return d["choices"][0]["message"]["content"] or "", (u.get("prompt_tokens", 0), u.get("completion_tokens", 0))


def _extract_json(raw: str) -> dict:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise ValueError(f"AI did not return JSON: {raw[:120]}")
    return json.loads(m.group(0))


def _num(v):
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def validate(d: dict) -> dict:
    """Clean the model's answer into a strict, safe structure."""
    kind = d.get("kind") if d.get("kind") in ("new_signal", "add_entries", "manage", "info") else "info"
    out = {"kind": kind, "confidence": max(0.0, min(1.0, _num(d.get("confidence")) or 0.0)),
           "reason": str(d.get("reason") or "")[:160], "signal": None, "actions": []}
    s = d.get("signal") or {}
    if kind in ("new_signal", "add_entries") and isinstance(s, dict):
        side = str(s.get("side") or "").lower()
        entries = [x for x in (_num(e) for e in (s.get("entries") or [])) if x][:4]
        out["signal"] = {"symbol": (str(s["symbol"]).upper().replace("/", "") if s.get("symbol") else None),
                         "side": side if side in ("buy", "sell") else None, "entries": entries,
                         "sl": _num(s.get("sl")), "tps": [x for x in (_num(t) for t in (s.get("tps") or [])) if x][:6]}
        if not out["signal"]["side"] or (kind == "new_signal" and (not out["signal"]["symbol"] or not entries)):
            out["kind"], out["signal"] = "info", None
    for a in (d.get("actions") or [])[:4]:
        if not isinstance(a, dict) or a.get("type") not in ACTIONS:
            continue
        frac = _num(a.get("fraction"))
        out["actions"].append({"type": a["type"], "fraction": min(max(frac, 0.05), 1.0) if frac else None,
                               "price": _num(a.get("price")),
                               "level": a.get("level") if a.get("level") in LEVELS else None})
    if kind == "manage" and not out["actions"]:
        out["kind"] = "info"
    return out


def to_trade_objects(ai: dict):
    """AI answer -> (Signal | None, [Command])."""
    sig = None
    if ai.get("signal"):
        s = ai["signal"]
        entries = s["entries"]
        lo, hi = (min(entries), max(entries)) if entries else (None, None)
        entry = (hi if s["side"] == "buy" else lo) if entries else None
        sig = Signal(s["symbol"], s["side"], "market", entry, (lo, hi) if len(entries) > 1 else None, s["sl"],
                     s["tps"], entries if len(entries) > 1 else [], ai["kind"] == "add_entries")
    cmds = []
    for a in ai.get("actions", []):
        t = a["type"]
        if t == "close_partial":
            cmds.append(Command("close_partial", value=a["fraction"] or 0.5))
        elif t == "move_sl":
            if a["level"] and a["level"].startswith("tp"):
                cmds.append(Command("move_sl", ref=a["level"]))
            elif a["level"]:
                cmds.append(Command("breakeven", ref=a["level"]))
            elif a["price"]:
                cmds.append(Command("move_sl", value=a["price"]))
        elif t == "move_tp" and a["price"]:
            cmds.append(Command("move_tp", value=a["price"]))
        elif t == "breakeven":
            cmds.append(Command("breakeven", ref=a["level"] if a["level"] and a["level"].startswith("layer") else None))
        elif t == "cancel_pending":
            cmds.append(Command("cancel"))
        elif t in ("close_all", "close_worst"):
            cmds.append(Command(t))
    return sig, cmds
