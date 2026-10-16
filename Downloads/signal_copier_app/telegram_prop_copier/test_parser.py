"""Run: python test_parser.py  - add your channel's real messages to CASES to check they're read correctly."""
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
from signal_parser import Command as C, Signal, parse

CASES = [
    # ---- new signals
    ("XAUUSD BUY @ 2350\nSL 2340\nTP1 2360\nTP2 2370", Signal("XAUUSD", "buy", "market", 2350, None, 2340, [2360, 2370])),
    ("🔥 GOLD SELL NOW 2,351.5 🔥\nSL: 2361.5\nTP: 2341.5\nTP: 2331.5", Signal("XAUUSD", "sell", "market", 2351.5, None, 2361.5, [2341.5, 2331.5])),
    ("EUR/USD BUY LIMIT 1.0850\nStop Loss 1.0820\nTake Profit 1.0910", Signal("EURUSD", "buy", "limit", 1.085, None, 1.082, [1.091])),
    ("US30 sell 39250-39280 sl 39350 tp 39100 tp 39000", Signal("US30", "sell", "market", 39250.0, (39250, 39280), 39350, [39100, 39000], [39250, 39280])),
    # ---- management (single action)
    ("Move SL to BE", [C("breakeven")]),
    ("Go break even on this one", [C("breakeven")]),
    ("Set SL to entry", [C("breakeven")]),
    ("Make it risk free now", [C("breakeven")]),
    ("close half now", [C("close_partial", None, 0.5)]),
    ("Secure 30% profits", [C("close_partial", None, 0.3)]),
    ("Take partials", [C("close_partial", None, 0.5)]),
    ("Close all gold trades", [C("close_all", "XAUUSD")]),
    ("Close the rest", [C("close_all")]),
    ("Exit now", [C("close_all")]),
    ("TP1 hit ✅ +100 pips", [C("tp_hit", None, 1.0)]),
    ("move sl to 2355", [C("move_sl", None, 2355.0)]),
    ("Move SL to TP1", [C("move_sl", None, None, "tp1")]),
    ("Cancel the EURUSD limit", [C("cancel", "EURUSD")]),
    # ---- several actions in one message
    ("TP1 hit ✅ close half and move SL to BE", [C("close_partial", None, 0.5), C("breakeven")]),
    ("TP2 smashed! Close 50%, SL to TP1", [C("close_partial", None, 0.5), C("move_sl", None, None, "tp1")]),
    ("TP2 hit. Close everything now", [C("close_all")]),
    # ---- chatter that must NOT trigger anything
    ("Good morning traders, big week ahead", None),
    ("Price is close to entry, be patient", None),
    ("Don't close yet, we're holding for TP2", None),
    ("If we get a daily close above 2360 we go higher", None),
    ("Waiting for H4 candle close before we exit", None),
    ("Our gold buy from yesterday hit TP2 +80 pips 🔥", None),
    ("We might close early before NFP", None),
    ("GOLD BUY 2350 SL 2360", None),   # SL on the wrong side -> rejected

    # ================= real messages from ATM Signals (Oct 2026) =================
    ("XAUUSD BUY 4139//4129\n\nTP1. @ 4143\nTP2. @ 4147\nTP3. @ 4151\nTP4. @ 4155\nTP5. @ 4159\nTP6. @ 4900\n\nSL     @ 4119",
     Signal("XAUUSD", "buy", "market", 4139, (4129, 4139), 4119, [4143, 4147, 4151, 4155, 4159, 4900], [4139, 4129])),
    ("XAUUSD Sell 4141//4151\n\nTP1. @ 4137\nTP2. @ 4133\nTP3. @ 4129\nTP4. @ 4125\nTP5. @ 4121\nTP6. @ 4900\n\nSL     @ 4161",
     Signal("XAUUSD", "sell", "market", 4141, (4141, 4151), 4161, [4137, 4133, 4129, 4125, 4121], [4141, 4151])),
    ("Add More Sell 4177//4183\n\nSame Target\n\nUpdate SL 4193",
     Signal(None, "sell", "market", 4177, (4177, 4183), 4193, [], [4177, 4183], True)),
    ("Set Breakeven at lower part now", [C("breakeven", ref="layer_low")]),
    ("Let's Set Breakeven now at Lower Part", [C("breakeven", ref="layer_low")]),
    ("Set Breakevn at Higher Layer now", [C("breakeven", ref="layer_high")]),
    ("Both Limits Active Guys\n\nSet Breakevn at 2nd Zone Now", [C("breakeven", ref="layer2")]),
    ("Close Wrost Enteries now", [C("close_worst")]),
    ("If you have worst Entires here\n\nYou can close Wrost Enteries now", [C("close_worst")]),
    ("Hold Only High Layers & Set Breakevn Make It Risk free Trade", [C("breakeven")]),
    ("#XAUUSD\nTARGET1 HIT 70+PIPS PROFIT DONE ", [C("tp_hit", "XAUUSD", 1.0)]),
    ("#XAUUSD\nTARGET4 HIT 220+PIPS PROFIT DONE ", [C("tp_hit", "XAUUSD", 4.0)]),
    # info / chatter from the same channel - must do nothing
    ("𝗚𝗢𝗟𝗗 BUY 𝗥𝗨𝗡𝗡𝗜𝗡𝗚 30+𝗣𝗶𝗽𝘀 𝗣𝗿𝗼𝗳𝗶𝘁", None),
    ("𝗚𝗢𝗟𝗗 Sell 𝗥𝗨𝗡𝗡𝗜𝗡𝗚 100+𝗣𝗶𝗽𝘀 𝗣𝗿𝗼𝗳𝗶𝘁", None),
    ("Looking Risky", None),
    ("We Need Only Risk free Trade Guys", None),
    ("We're Current looking for risk free trade but there is no confirmation stay connected we'll drop set up", None),
    ("Already 4152 Done", None),
    ("BE HIT", None),
    ("Again Breakeven Hit", None),
    ("Xauusd buy now", None),
    ("Enter now", None),
    ("[ 🧠 Sticker ]", None),
    ("We got hit 2 SL of start of week . both first 2 trades\n\nStrong Comeback", None),
    ("( ATM 🏧 ) WEEKLY SUMMARY \n\n#MONDAY✅ \n\n1: WON 860+PIPS\n         ( 2 SL  -400 PIPS )\n\nTOTAL: 6230 PIPS WON", None),
    ("TUESDAY' S  RESULTS ✅ \n\nGOLD BUY 120+PIPS  ✅ (R, T) \nGOLD SELL 100+PIPS ✅(BE HIT) \nGOLD SELL 370+PIPS ✅", None),
    ("MONDAY' S  RESULTS ✅ \n\nGOLD BUY 100+PIPS  ✅( BE ) \nGOLD BUY 460+PIPS  ✅\n\nToday We Win 890+PIPS", None),
    # needs judgment (typo "not" for "now", corrections) - left for the AI interpreter, rules do nothing
    ("Set Breakevn not at higher layer", None),
    ("Correct Second Entery 4169 ✅", None),
]

fails = 0
for text, want in CASES:
    got = parse(text)
    ok = got == want
    fails += not ok
    print(("PASS " if ok else "FAIL ") + repr(text[:50]) + ("" if ok else f"\n   got:  {got}\n   want: {want}"))
print(f"\n{len(CASES) - fails}/{len(CASES)} passed")
