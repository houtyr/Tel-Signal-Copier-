"""Telegram connection: login from the UI, chat listing, and forwarding monitored messages to the engine."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

log = logging.getLogger("telegram")
ROOT = Path(__file__).parent


class TelegramManager:
    def __init__(self, settings, engine, bus):
        self.settings = settings
        self.engine = engine
        self.bus = bus
        self.client = None
        # not_configured | connecting | login_required | code_required | password_required | connected | error
        self.status = "not_configured"
        self.error = ""
        self.me = ""
        self._phone_hash = None
        self._handler_added = False

    def view(self) -> dict:
        return {"status": self.status, "error": self.error, "me": self.me,
                "phone": self.settings["telegram"].get("phone", "")}

    def _emit(self):
        self.bus.emit("telegram", self.view())

    def _set(self, status, error=""):
        self.status, self.error = status, error
        self._emit()

    # ---------- connect / login ----------
    async def start(self):
        tg = self.settings.telegram()
        if not tg.get("api_id") or not tg.get("api_hash"):
            self._set("not_configured")
            return
        try:
            from telethon import TelegramClient
        except ImportError:
            self._set("error", "telethon not installed: pip install telethon")
            return
        await self.stop()
        self._set("connecting")
        try:
            self.client = TelegramClient(str(ROOT / tg.get("session", "copier")), int(tg["api_id"]), tg["api_hash"])
            await self.client.connect()
            if await self.client.is_user_authorized():
                await self._ready()
            else:
                self._set("login_required")
        except Exception as e:
            self._set("error", str(e))

    async def stop(self):
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass
        self.client = None
        self._handler_added = False

    async def send_code(self, phone: str | None = None):
        phone = phone or self.settings["telegram"].get("phone")
        if not self.client:
            await self.start()
        sent = await self.client.send_code_request(phone)
        self._phone_hash = sent.phone_code_hash
        self._set("code_required")

    async def submit_code(self, code: str):
        from telethon.errors import SessionPasswordNeededError
        try:
            await self.client.sign_in(phone=self.settings["telegram"]["phone"], code=code.strip(),
                                      phone_code_hash=self._phone_hash)
        except SessionPasswordNeededError:
            self._set("password_required")
            return
        await self._ready()

    async def submit_password(self, password: str):
        await self.client.sign_in(password=password)
        await self._ready()

    async def logout(self):
        if self.client:
            await self.client.log_out()
        await self.stop()
        self.me = ""
        self._set("login_required" if self.settings["telegram"].get("api_id") else "not_configured")

    async def _ready(self):
        from telethon import events
        me = await self.client.get_me()
        self.me = (me.first_name or "") + (f" (@{me.username})" if me.username else "")
        if not self._handler_added:
            self.client.add_event_handler(self._on_new, events.NewMessage())
            self._handler_added = True
        self._set("connected")
        asyncio.create_task(self._watch_connection())

    async def _watch_connection(self):
        while self.client:
            await asyncio.sleep(10)
            if self.client and not self.client.is_connected() and self.status == "connected":
                self._set("connecting", "reconnecting...")
                try:
                    await self.client.connect()
                    self._set("connected")
                except Exception as e:
                    self._set("error", str(e))

    # ---------- chats ----------
    async def dialogs(self) -> list[dict]:
        if self.status != "connected":
            return []
        out = []
        async for d in self.client.iter_dialogs(limit=300):
            if d.is_user and not d.entity.bot:
                continue  # private chats with people aren't signal sources
            out.append({"id": d.id, "title": d.name, "type": "channel" if d.is_channel and not d.is_group
                        else "group" if d.is_group else "bot", "unread": d.unread_count})
        return out

    def monitored(self) -> dict[int, str]:
        return {int(c["id"]): c.get("title", "") for c in self.settings["channels"] if c.get("enabled", True)}

    async def _on_new(self, event):
        chats = self.monitored()
        if event.chat_id not in chats:
            return
        m = event.message
        text = m.message or ""
        if not text.strip():
            return
        reply_to = m.reply_to.reply_to_msg_id if m.reply_to else None
        try:
            await self.engine.on_message(event.chat_id, chats[event.chat_id], m.id, reply_to, text, m.date)
        except Exception:
            log.exception("message handling failed")
