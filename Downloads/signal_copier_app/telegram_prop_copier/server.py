"""Web server for the Signal Copier dashboard. Start it with `python app.py`."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
import webbrowser
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from engine import Bus, Engine
from settings import Settings
from signal_parser import parse
from store import Store
from telegram_manager import TelegramManager

ROOT = Path(__file__).parent
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(ROOT / "copier.log", encoding="utf-8")])

settings = Settings(ROOT / "settings.json")
store = Store(ROOT / "copier.db")
bus = Bus()
engine = Engine(settings, store, bus)
telegram = TelegramManager(settings, engine, bus)
TEST_CHAT_ID = 0


@asynccontextmanager
async def lifespan(_app):
    await engine.start()
    asyncio.create_task(telegram.start())
    yield
    await telegram.stop()
    await engine.stop()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


def ok(**kw):
    return JSONResponse({"ok": True, **kw})


async def guarded(coro):
    try:
        return await coro
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, str(e))


# ------------------------------------------------------------------ page + live feed
@app.get("/")
async def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.websocket("/ws")
async def ws(sock: WebSocket):
    await sock.accept()
    q = bus.subscribe()
    try:
        while True:
            await sock.send_text(await q.get())
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        bus.unsubscribe(q)


@app.get("/api/state")
async def state():
    return {"settings": settings.public(), "telegram": telegram.view(), **engine.snapshot(),
            "ai": engine.ai.stats(),
            "messages": store.messages(150), "events": store.events(200), "trades": store.trades(200),
            "server_time": time.time()}


@app.get("/api/equity")
async def equity(hours: float = 24):
    return store.equity_series(time.time() - hours * 3600)


# ------------------------------------------------------------------ settings
@app.post("/api/settings")
async def update_settings(patch: dict = Body(...)):
    settings.update_global(patch)
    bus.emit("settings", settings.public())
    return ok()


# ------------------------------------------------------------------ accounts
@app.post("/api/accounts")
async def upsert_account(body: dict = Body(...)):
    acc = body.get("account") or {}
    if not acc.get("name") or not acc.get("platform"):
        raise HTTPException(400, "name and platform are required")
    await guarded(engine.upsert_account(acc, body.get("original_name")))
    bus.emit("settings", settings.public())
    return ok()


@app.delete("/api/accounts/{name}")
async def delete_account(name: str):
    await engine.delete_account(name)
    store.delete_account(name)
    bus.emit("settings", settings.public())
    return ok()


@app.post("/api/accounts/{name}/{action}")
async def account_action(name: str, action: str, body: dict = Body(default={})):
    if not settings.account(name):
        raise HTTPException(404, "no such account")
    if action == "connect":
        await engine.connect(name)
    elif action == "disconnect":
        await engine.disconnect(name)
    elif action == "copy":
        a = settings.account(name)
        settings.upsert_account({**a, "copy_enabled": bool(body.get("enabled"))}, name)
        engine.note("info", name, f"copying {'enabled' if body.get('enabled') else 'paused'} for this account")
        engine.emit_account(engine.accounts[name])
    elif action == "close_all":
        await guarded(engine.close_all(name))
    elif action == "close":
        await guarded(engine.close_position(name, int(body["ticket"]), body.get("volume")))
    elif action == "mirror":
        a = settings.account(name)
        settings.upsert_account({**a, "mirror_enabled": bool(body.get("enabled"))}, name)
        engine.note("info", name, f"mirroring {'resumed' if body.get('enabled') else 'paused'} for this account")
        engine.emit_account(engine.accounts[name])
    elif action in ("paper_order", "paper_modify"):
        # Manual trades are only offered on simulated accounts - for testing mirroring
        if settings.account(name).get("platform") != "paper":
            raise HTTPException(400, "manual test trades are only allowed on Paper accounts")
        acc = engine.accounts[name]
        if action == "paper_order":
            sym = body["symbol"].upper()
            t = await guarded(acc.call("open", sym, body["side"], float(body["volume"]), "market", None,
                                       float(body["sl"]) if body.get("sl") else None,
                                       float(body["tp"]) if body.get("tp") else None, "manual"))
            engine.note("ok", name, f"test trade: {body['side'].upper()} {sym} {float(body['volume']):.2f} lots (#{t})")
        else:
            await guarded(acc.call("modify", int(body["ticket"]),
                                   sl=float(body["sl"]) if body.get("sl") not in (None, "") else None,
                                   tp=float(body["tp"]) if body.get("tp") not in (None, "") else None))
        await engine._poll(acc)
    elif action == "resume":
        engine.accounts[name].halted = ""
        engine.note("warn", name, "halt cleared manually - copying resumed")
        engine.emit_account(engine.accounts[name])
    else:
        raise HTTPException(404, "unknown action")
    return ok()


@app.post("/api/flatten_all")
async def flatten_all():
    await engine.flatten_everything()
    return ok()


# ------------------------------------------------------------------ telegram
@app.post("/api/telegram/config")
async def tg_config(body: dict = Body(...)):
    settings.set_telegram(body)
    await telegram.start()
    return ok(telegram=telegram.view())


@app.post("/api/telegram/send_code")
async def tg_send_code():
    await guarded(telegram.send_code())
    return ok()


@app.post("/api/telegram/code")
async def tg_code(body: dict = Body(...)):
    await guarded(telegram.submit_code(str(body.get("code", ""))))
    return ok()


@app.post("/api/telegram/password")
async def tg_password(body: dict = Body(...)):
    await guarded(telegram.submit_password(str(body.get("password", ""))))
    return ok()


@app.post("/api/telegram/logout")
async def tg_logout():
    await guarded(telegram.logout())
    return ok()


@app.get("/api/telegram/dialogs")
async def tg_dialogs():
    return await guarded(telegram.dialogs())


@app.post("/api/channels")
async def set_channels(body: dict = Body(...)):
    settings.set_channels(body.get("channels", []))
    bus.emit("settings", settings.public())
    return ok()


# ------------------------------------------------------------------ AI interpreter + review queue
@app.post("/api/ai")
async def ai_config(body: dict = Body(...)):
    settings.set_ai(body)
    bus.emit("settings", settings.public())
    return ok()


@app.post("/api/ai/test")
async def ai_test(body: dict = Body(...)):
    """Ask the configured model to read a message - used by the Settings test button and test console."""
    from interpreter import to_trade_objects
    from engine import describe_ai
    c = settings.ai()
    if not c.get("model") or (not c.get("api_key") and "localhost" not in (c.get("base_url") or "")):
        raise HTTPException(400, "Add a model and API key first (Settings → AI interpreter)")
    t0 = time.time()
    ai = await guarded(engine.ai.interpret(body.get("text", ""), body.get("context") or
                                          "Not a reply. There are no open copied trades from this channel."))
    return {"ai": ai, "summary": describe_ai(ai), "ms": int((time.time() - t0) * 1000)}


@app.post("/api/messages/{mid}/{decision}")
async def review(mid: int, decision: str):
    if decision not in ("approve", "dismiss"):
        raise HTTPException(404, "unknown decision")
    await guarded(engine.apply_review(mid, decision == "approve"))
    return ok()


# ------------------------------------------------------------------ testing
@app.get("/api/parse")
async def parse_preview(text: str):
    from dataclasses import asdict
    p = parse(text, {k.upper(): v.upper() for k, v in (settings.get("symbol_aliases") or {}).items()})
    if isinstance(p, list):
        return {"kind": "command", "parsed": [asdict(c) for c in p]}
    return {"kind": "signal" if p else "none", "parsed": asdict(p) if p else None}


_test_counter = [int(time.time()) % 100000 * 10]


@app.post("/api/test_message")
async def test_message(body: dict = Body(...)):
    """Feed a message to the engine as if it came from Telegram (chat 'Test')."""
    _test_counter[0] += 1
    chat_id = int(body.get("chat_id") or TEST_CHAT_ID)
    title = "Test console" if chat_id == TEST_CHAT_ID else next(
        (c["title"] for c in settings["channels"] if int(c["id"]) == chat_id), str(chat_id))
    reply_to = int(body["reply_to"]) if body.get("reply_to") else None
    asyncio.create_task(engine.on_message(chat_id, title, _test_counter[0], reply_to, body.get("text", ""),
                                          datetime.now(timezone.utc)))
    return ok(msg_id=_test_counter[0])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    if not args.no_browser:
        import threading
        threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}")).start()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")

