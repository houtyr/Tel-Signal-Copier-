"""
Parses Telegram signal messages into structured trade instructions.

Handles two kinds of messages:
  1. New signals   e.g. "XAUUSD BUY @ 2350  SL 2340  TP1 2360  TP2 2370"
  2. Management    e.g. "Move SL to BE", "Close half", "Close all gold", "TP1 hit"

Management messages are usually sent as a *reply* to the original signal;
engine.py uses the reply link to know which trade they refer to.

Zone signals like "XAUUSD BUY 4139//4129" are read as two entry LAYERS: the first price is the
near-market layer, the second is the deeper layer (a limit order).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

NUM = r"(\d+(?:\.\d+)?)"

# Common names channels use -> canonical symbol. Extend in config.yaml (symbol_aliases).
DEFAULT_ALIASES = {
    "GOLD": "XAUUSD", "XAU": "XAUUSD", "SILVER": "XAGUSD",
    "US30": "US30", "DOW": "US30", "DJ30": "US30",
    "NAS100": "NAS100", "NAS": "NAS100", "NASDAQ": "NAS100", "US100": "NAS100", "USTEC": "NAS100",
    "SPX500": "SPX500", "US500": "SPX500", "SP500": "SPX500",
    "GER40": "GER40", "DAX": "GER40", "DE40": "GER40",
    "BTC": "BTCUSD", "BITCOIN": "BTCUSD", "ETH": "ETHUSD",
    "OIL": "USOIL", "WTI": "USOIL", "USOIL": "USOIL",
}

FX_CCY = {"USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF", "XAU", "XAG"}


@dataclass
class Signal:
    symbol: str
    side: str                       # "buy" | "sell"
    order_type: str = "market"      # "market" | "limit" | "stop"
    entry: Optional[float] = None   # single price or midpoint of a range
    entry_range: Optional[tuple[float, float]] = None
    sl: Optional[float] = None
    tps: list[float] = field(default_factory=list)
    layers: list[float] = field(default_factory=list)   # entry prices in the order written ("4139//4129")
    add_on: bool = False            # "Add more sell 4177//4183 same target" - no symbol, belongs to the replied signal


@dataclass
class Command:
    action: str                     # close_all | close_partial | breakeven | move_sl | move_tp | cancel | tp_hit
    symbol: Optional[str] = None
    value: Optional[float] = None   # price for move_sl/move_tp, fraction for close_partial, tp number for tp_hit
    ref: Optional[str] = None       # "tp1"/"tp2"... when the SL moves to a TP level of the original signal


def _clean(text: str) -> str:
    # NFKC turns styled text like "𝗚𝗢𝗟𝗗 𝗥𝗨𝗡𝗡𝗜𝗡𝗚" into plain "GOLD RUNNING"
    t = unicodedata.normalize("NFKC", text).upper()
    t = t.replace("’", "'")
    t = re.sub(r"(?<=\d),(?=\d{3}\b)", "", t)          # 2,350.5 -> 2350.5
    t = re.sub(r"[^\w\s.@:=/\-%,;!?&]", " ", t)          # drop emojis / most punctuation
    return re.sub(r"[ \t]+", " ", t)


def find_symbol(text: str, aliases: dict[str, str]) -> Optional[str]:
    t = _clean(text)
    # 6-letter FX / metal pairs, optionally written EUR/USD
    for m in re.finditer(r"\b([A-Z]{3})\s?/?\s?([A-Z]{3})\b", t):
        a, b = m.group(1), m.group(2)
        if a in FX_CCY and b in FX_CCY and a != b:
            return a + b
    for word in re.findall(r"\b[A-Z][A-Z0-9]{1,9}\b", t):
        if word in aliases:
            return aliases[word]
    return None


def parse(text: str, aliases: Optional[dict[str, str]] = None) -> Optional[Signal | list[Command]]:
    """Returns a Signal, a non-empty list of Commands (one message can hold several,
    e.g. "TP1 hit - close half and move SL to BE"), or None for chatter."""
    aliases = {**DEFAULT_ALIASES, **(aliases or {})}
    t = _clean(text)

    sig = _parse_signal(t, text, aliases)
    if sig:
        return sig
    cmds = parse_commands(t, text, aliases)
    return cmds or None


def _parse_signal(t: str, raw: str, aliases) -> Optional[Signal]:
    side_m = re.search(r"\b(BUY|SELL|LONG|SHORT)\b(?:\s+(LIMIT|STOP|NOW))?", t)
    if not side_m:
        return None
    sl_m = re.search(r"\b(?:SL|S/L|STOP\s?LOSS|STOPLOSS)[\s.:@=]*" + NUM, t)
    tp_list = re.findall(r"\b(?:TP|T/P|TAKE\s?PROFIT|TARGET)(?:\s?\d\b(?!\.\d))?[\s.:@=\-]*" + NUM, t)
    # A signal needs at least an SL or a TP; otherwise "buy" is just chatter.
    if not sl_m and not tp_list:
        return None
    # Result posts ("our gold buy hit TP2 +80 pips", weekly summaries) mention BUY and TP but are not new trades
    if not sl_m and re.search(r"\b(HIT|SMASHED|REACHED|RUNNING|PIPS|RESULTS?|RECAP|BOOKED|SECURED)\b", t):
        return None
    if re.search(r"\b(RESULTS?|SUMMARY|OVERALL|TOTAL|WON|RECAP)\b", t):
        return None
    symbol = find_symbol(raw, aliases)
    add_on = False
    if not symbol:
        # "Add more sell 4177//4183, same target, update SL 4193" - a re-entry on the signal it replies to
        if re.search(r"\b(ADD|ADDING|RE-?ENTER|RE-?ENTRY)\b", t):
            add_on = True
        else:
            return None

    side = "buy" if side_m.group(1) in ("BUY", "LONG") else "sell"
    kind = side_m.group(2)
    order_type = {"LIMIT": "limit", "STOP": "stop"}.get(kind or "", "market")

    # Entry: "@ 2350", "ENTRY 2350", "2350-2345", or the first number right after BUY/SELL
    entry, entry_range, layers = None, None, []
    after_side = t[side_m.end():]
    rng = re.search(r"(?:@|ENTRY|PRICE|AT|ZONE)?\s*[:=]?\s*" + NUM + r"\s*(?:-|//?|TO|AND)\s*" + NUM, after_side)
    single = re.search(r"(?:@|ENTRY|PRICE|AT)\s*[:=]?\s*" + NUM, t) or re.match(r"\s*" + NUM, after_side)
    if rng and not re.search(r"\b(SL|TP)\b", after_side[: rng.start()]):
        a, b = float(rng.group(1)), float(rng.group(2))
        entry_range = (min(a, b), max(a, b))
        layers = [a, b]
        # the near-market edge: highest price for a buy zone, lowest for a sell zone
        entry = max(a, b) if side_m.group(1) in ("BUY", "LONG") else min(a, b)
    elif single:
        entry = float(single.group(1))

    sl = float(sl_m.group(1)) if sl_m else None
    tps = [float(x) for x in tp_list]

    # Sanity: SL must be on the losing side, TPs on the winning side of entry
    ref = entry if entry is not None else (tps[0] if tps else None)
    if sl is not None and entry is not None:
        if (side == "buy" and sl >= entry) or (side == "sell" and sl <= entry):
            return None
    if entry is not None:
        tps = [tp for tp in tps if (tp > entry if side == "buy" else tp < entry)]
    _ = ref
    return Signal(symbol, side, order_type, entry, entry_range, sl, tps, layers, add_on)


# ------------------------------------------------------------------ management commands
# Words that make a clause conditional, negated or hypothetical - those are never executed.
_HEDGE = re.compile(r"\b(DON\s?T|DO NOT|NOT|NO NEED|NEVER|WAIT|HOLD|AVOID|IF|WHEN|ONCE|UNLESS|MIGHT|MAY|WOULD|COULD|"
                    r"SHOULD WE|THINKING|CONSIDER|PLANNING|ABOUT TO|TOMORROW|LATER|LOOKING|WILL|WE LL|STAY CONNECTED)\b")
# Common misspellings seen in channels
_BE = r"(?:BE|B\s?E|B/E|BREAK\s?EVEN|BREAKEVEN|BREAK\s?EVN|BREAKEVN|BRK\s?EVEN|BREAKEVAN)"
_WORST = r"(?:WORST|WROST|WORSE|WOSRT|BAD)\s+(?:ENTRIES|ENTERIES|ENTIRES|ENTRYS|ENTRY|ENTERY|LAYERS?|POSITIONS?|TRADES?)"
_LAYER = [  # phrase -> which entry layer a breakeven/SL instruction refers to
    (r"\b(?:LOWER|LOW|BOTTOM)\s+(?:PART|LAYER|ZONE|ENTRY|ENTERY|LEVEL|SIDE)", "layer_low"),
    (r"\b(?:HIGHER|HIGH|UPPER|TOP)\s+(?:PART|LAYER|ZONE|ENTRY|ENTERY|LEVEL|SIDE)", "layer_high"),
    (r"\b(?:2ND|SECOND)\s+(?:ZONE|LAYER|ENTRY|ENTERY|LIMIT|PART)", "layer2"),
    (r"\b(?:1ST|FIRST)\s+(?:ZONE|LAYER|ENTRY|ENTERY|PART)", "layer1"),
]
# "close above 2350", "daily close", "close to entry" talk about candles/price, not orders
_CANDLE = re.compile(r"\bCLOS(E|ED|ING)\s+(ABOVE|BELOW|UNDER|OVER|TO|NEAR|BY|ON|OF|AT\s+THE|THE\s+(DAY|WEEK|CANDLE|GAP))\b|"
                     r"\b(DAILY|WEEKLY|CANDLE|H1|H4|M15|BAR)\s+CLOS")
_SPLIT = re.compile(r"\n|[.;!?]\s|,\s*|\s+(?:AND|THEN|PLUS|ALSO|&)\s+|\s+-\s+")


def _clauses(t: str) -> list[str]:
    return [c.strip(" .-:") for c in _SPLIT.split(t) if c and c.strip(" .-:")]


def parse_commands(t: str, raw: str, aliases) -> list[Command]:
    symbol = find_symbol(raw, aliases)
    out: list[Command] = []
    for c in _clauses(t):
        if _HEDGE.search(c) or _CANDLE.search(c):
            continue
        cmd = _clause_command(c)
        if cmd and not any(o.action == cmd.action and o.value == cmd.value for o in out):
            cmd.symbol = symbol
            out.append(cmd)
    # An explicit instruction beats the generic "TP hit" (which would otherwise move SL to BE)
    if len(out) > 1:
        out = [o for o in out if o.action != "tp_hit"] or out
    # Closing everything makes other instructions in the same message moot
    if any(o.action == "close_all" for o in out):
        out = [o for o in out if o.action in ("close_all", "cancel")]
    return out


def _clause_command(c: str) -> Optional[Command]:
    words = c.split()
    # --- partial close: "close half", "take partials", "secure 50%", "close 30%"
    m = re.search(r"\b(?:CLOSE|CLOSED|CLOSING|TAKE|TOOK|SECURE|SECURED|BOOK|BOOKED|LOCK)\s+(?:IN\s+)?(\d{1,2})\s?%", c)
    if m:
        return Command("close_partial", value=int(m.group(1)) / 100)
    if re.search(r"\b(?:CLOSE|CLOSED|TAKE|TOOK|SECURE|SECURED|BOOK|BOOKED|LOCK)\s+(?:SOME\s+)?(?:HALF|PARTIALS?|SOME(?:\s+PROFITS?)?|PROFITS?)\b", c) \
            or (re.fullmatch(r"(?:TAKE\s+)?PARTIALS?(?:\s+NOW)?", c)):
        return Command("close_partial", value=0.5)
    # --- close only the worse-priced entry layer: "close worst entries"
    if re.search(r"\b(?:CLOSE|CUT|EXIT)\b.*\b" + _WORST, c):
        return Command("close_worst")
    # --- full close: verb near the start of a short clause
    if len(words) <= 10 and re.search(r"^(?:PLEASE\s+)?(?:CLOSE|CLOSED|EXIT|EXITED|GET\s+OUT|CUT|KILL)\b", c) \
            or re.search(r"\bCLOSE\s+(?:ALL|EVERYTHING|THE\s+REST|REST|REMAINING|FULL|FULLY|NOW)\b", c):
        if not re.search(r"\b(HALF|PARTIAL|SOME)\b", c):
            return Command("close_all")
    # --- SL to a TP level of the original signal: "move SL to TP1"
    m = re.search(r"\b(?:SL|STOPS?|STOP\s?LOSS)\b.*?\b(?:TO|@|AT)\s+TP\s?(\d)\b", c)
    if m:
        return Command("move_sl", ref=f"tp{m.group(1)}")
    # --- breakeven / risk free (optionally "at the lower layer" / "at 2nd zone")
    if re.search(r"\b(?:SL|STOPS?|STOP\s?LOSS|MOVE|SET|PUT)\b.*\b(?:" + _BE[3:-1] + r"|ENTRY|OPEN\s+PRICE|ENTRY\s+PRICE)\b", c) \
            or re.search(r"\b(?:MAKE|SET|GO|KEEP)\b.*\bRISK\s?-?FREE\b|^" + _BE + r"(?:\s+NOW)?$", c) \
            or re.search(r"\b(?:GO|MOVE\s+TO|SET|PUT\s+IT\s+(?:AT|TO))\s+" + _BE + r"\b", c):
        layer = next((name for pat, name in _LAYER if re.search(pat, c)), None)
        return Command("breakeven", ref=layer)
    # --- move SL / TP to a price
    m = re.search(r"\b(?:MOVE|SET|NEW|CHANGE|ADJUST|TRAIL|UPDATE|UPDATED)\s+(?:THE\s+)?(?:SL|STOPS?|STOP\s?LOSS)\s*(?:TO|@|:|=|AT)?\s*" + NUM, c) or \
        re.search(r"\b(?:SL|STOPS?|STOP\s?LOSS)\s+(?:TO|NOW|MOVED\s+TO|@|AT)\s*" + NUM, c)
    if m:
        return Command("move_sl", value=float(m.group(1)))
    m = re.search(r"\b(?:MOVE|SET|NEW|CHANGE|ADJUST)\s+(?:THE\s+)?TP\s*\d?\s*(?:TO|@|:|=|AT)?\s*" + NUM, c)
    if m:
        return Command("move_tp", value=float(m.group(1)))
    # --- cancel a pending order
    if re.search(r"\b(?:CANCEL|CANCELLED|DELETE|REMOVE)\b", c) and (len(words) <= 8 or re.search(r"\b(ORDER|LIMIT|PENDING|ENTRY|SIGNAL)\b", c)):
        return Command("cancel")
    # --- TP hit reports: "TP1 hit", "TP2 done", "TP1" on its own
    m = re.search(r"\b(?:TP|TARGET|TGT)\s?(\d)?\s*(?:HIT|DONE|SMASHED|REACHED|SECURED|BOOKED)\b", c) or re.fullmatch(r"TP\s?(\d)", c)
    if m:
        return Command("tp_hit", value=float(m.group(1) or 1))
    return None
