# Signal Copier — Telegram → prop accounts dashboard

A local app that reads trade signals from your Telegram channels and copies them to several prop
accounts at once (MT5, TradeLocker, or simulated Paper accounts). You run it from a dashboard in your browser.

## Start it (Windows)
1. Install Python 3.11+ (tick "Add to PATH").
2. Double-click **start.bat**. The first run installs the requirements, then opens http://127.0.0.1:8765.
   (Or run: `pip install -r requirements.txt` then `python app.py`.)
3. Keep that window open while you want copying to run. The dashboard only works on this computer.

## First-time setup in the dashboard
1. **Accounts → Add account → Paper** to try everything with no risk.
2. **Telegram** tab: enter your API ID/hash from my.telegram.org, then **Send login code** and enter it.
   Then **Browse my chats** and tick your signal channels.
3. **Telegram → Test console**: paste real messages from your channel to check how they're read.
   Use ↩ on a signal to test replies like "move SL to BE" or "close half".
4. Add your real accounts (MT5 / TradeLocker). The app starts in **Dry run**, so it logs what it would do
   without placing orders. Click **Dry run → Go live** in the top bar when you're ready.

## Tabs
- **Overview**: total equity, today's P&L, an equity chart for each account, account cards (with
  daily-loss and drawdown meters and a copy on/off switch), all open positions with a close button, and the live signal feed.
- **Accounts**: add, edit or remove copy accounts. Each one has its own risk, prop limits, symbol mapping,
  and the channels it copies.
- **Telegram**: log in, choose channels, test console, message feed.
- **Activity**: every action, warning and error, plus the trade log.
- **Settings**: default risk and prop rules, daily reset time, symbol aliases, theme.

## Several accounts
- Every account runs in its own background process. One crashing doesn't affect the others.
- **More than one MT5 account:** the MetaTrader5 Python package can only drive one terminal per process.
  Install MT5 once per account in its own folder (e.g. `C:\MT5-FTMO1`, `C:\MT5-FTMO2`). Then set each
  account's *MT5 terminal path* to its own `terminal64.exe`. Log in to each terminal once and turn on **Algo Trading**.
- **Channel routing:** under Accounts → Channels, tick the channels an account should copy. Leave them all
  unticked to copy every monitored channel.

## Account-to-account copying (mirroring)
Copy trades from one account to others in any direction: MT5 → MT5, MT5 → TradeLocker, TradeLocker → MT5,
or one source to many followers. Edit the **follower** account and set **Mirror another account → Copy trades from**.
- Mirrors new trades, partial closes, SL/TP changes, and full closes, usually within 1–2 seconds.
- Sizing options: scaled to account size (default), same exposure, lot multiplier, fixed lots, or % risk using
  the source's SL. Sizing accounts for different contract sizes between brokers.
- Trades that were already open when you set up the link are left alone unless you tick "Also copy trades already open".
- If a follower was offline, it won't copy trades older than 30 seconds (change this in Settings).
- Followers still follow their own prop limits. An account can mirror one source and copy Telegram at the same
  time (switch Telegram off on its card if you don't want that). Copy loops (A → B → A) are blocked.
- Paper accounts have a **Test trade** button and an SL/TP edit button (✎), so you can try mirroring safely.

## How Telegram trade updates are handled
- **Replies/quotes** ("go break even" quoting a signal from hours ago) go to exactly that signal's trades.
  Replies to an update message (e.g. replying to "TP1 hit") also find the original signal.
- **Several instructions in one message** run in order: "TP1 hit, close half and move SL to BE" closes 50%, then moves SL to entry.
- Understood: close all / close the rest, close half or close N%, partials, breakeven / SL to entry / risk free,
  SL to a price, SL to TP1/TP2, TP to a price, cancel pending order, TP hit (moves runners to breakeven).
- **Ignored on purpose:** negated or conditional lines ("don't close yet", "if we close above 2360…"), candle talk
  ("H4 close", "close to entry"), and result posts ("gold hit TP2 +80 pips").
- **Updates that aren't replies** are applied only when exactly one open trade from that channel could be meant.
  Otherwise they're skipped and the feed says why. You can change this in Settings.
- An SL move that would be past the current price is not sent, and you get a warning instead.
- Every message in the feed shows how it was read and what each account did with it.

## AI interpreter (optional)
The rules read standard signals and updates. For messages they can't read (typos, "not" meant as "now",
corrections, unusual wording), a low-cost AI model can read them instead. Set it up in **Settings → AI interpreter**:

| Option | Cost | Notes |
|---|---|---|
| Claude Haiku 5.5 (`claude-haiku-5-5`) | ~$0.0002 per message read, about $1/month for a busy channel | Most reliable. Key from console.anthropic.com |
| Gemini Flash-Lite (`gemini-3.5-flash-lite`) | Free tier | Free key at aistudio.google.com. On the free tier Google may use prompts to improve its products |
| Groq (`openai/gpt-oss-20b`) | Free tier (~1,000 requests/day) | Very fast. Key from console.groq.com |
| Ollama on your PC | Free | Private, nothing leaves your computer. Slower and less accurate |

- It's only asked when the rules found nothing and the message is a reply to a copied trade or contains a trading word.
  "Running 60 pips" updates, results and summaries never go to the AI.
- Management actions above your confidence threshold (default 80%) run automatically. Anything below it,
  and every new trade or add-on the AI proposes, waits in the feed with **Approve / Dismiss**.
- The model can't place anything by itself. It returns a short JSON answer that the app checks against the same rules
  as everything else (prop limits, SL sanity, never past the current price).
- Use **Ask AI** in the test console, or **Test** in Settings, to see how it reads a message before going live.

## Zone signals and layers (e.g. ATM Signals)
- "XAUUSD BUY 4139//4129" = two entry layers: a market order near 4139 and a limit at 4129. Risk is split
  across both. Each layer is split across TP1–TP3 by default.
- "Set breakeven at lower part / higher layer / 2nd zone" moves the SL of that signal's trades to that layer's price.
- "Close worst entries" closes the worse-priced layer, but only if the better layer has also filled.
- "TARGET1 HIT" moves the SL to breakeven (or trails to the previous TP — see Settings).
- "Add more sell 4177//4183, same target, update SL 4193" (as a reply) adds entries at half risk, uses the original
  TPs, and moves the SL on the existing trades.
- "GOLD BUY RUNNING 60+ pips", "BE HIT", daily/weekly results, and "buy now / enter now" heads-ups are ignored.

## Safety features
- Daily-loss and max-drawdown stops sit below the firm's limits. When one is hit, the app closes copier
  trades and pauses that account until the daily reset (default 5 PM New York).
- **Copying on/off** pauses every account. **Close everything** closes all copier positions on every account.
- Skips stale signals, signals with no SL (by default), and signals where price is already past the SL.

## Before going live
- **Check your prop firm's rules on copy trading / third-party signals.** Many firms ban it or flag
  identical trades across traders, and it's a common reason payouts are denied.
- Compare symbol names with your Market Watch (`symbol suffix` / `symbol mapping`).
- TradeLocker: confirm the contract sizes (e.g. gold = 100). Place a 0.01 lot test trade on demo.
- Run a few days on a demo or free-trial account first.

## Files
| File | What it is |
|---|---|
| `app.py` / `server.py` | Start-up and the local web server |
| `engine.py` | Copy logic, P&L tracking, prop guard, breakeven management |
| `mirror.py` | Account-to-account copying |
| `interpreter.py` | Optional AI reading of unusual messages |
| `telegram_manager.py` | Telegram login and channel listener |
| `signal_parser.py` | How messages are read. Edit this for unusual formats. `test_parser.py` checks it |
| `risk.py` | Lot sizing and prop limits |
| `brokers/` | MT5, TradeLocker and Paper connectors |
| `static/` | The dashboard (HTML/CSS/JS) |
| `settings.json`, `copier.db`, `state.json` | Created on first run: your settings (including passwords), history, open-trade links |

Passwords are saved in `settings.json` on this computer. To keep them out of the file, enter
`${MY_ENV_VAR}` in the password field instead and set that environment variable in Windows.
