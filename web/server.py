"""sharpbet web app: a thin server around the same ChatSession that chat.py uses.

    python web/server.py            # http://127.0.0.1:8000 (landing), /chat (the assistant)

Local only (binds to 127.0.0.1). Each browser tab has its own conversation, held in memory,
with the same per-session spending cap as the terminal chat.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import queue
import threading
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from sharpbet.assistant import ChatSession, SpendCapReached, Stopped
from sharpbet.data import load_env

STATIC = Path(__file__).parent / "static"
# Browsers must re-check pages and assets every load (a cheap 304 when unchanged); otherwise an old
# cached app.js keeps running after an update (2026-09-28: comparison rows weren't clickable).
NO_CACHE = {"Cache-Control": "no-cache"}


def versioned(page: Path) -> str:
    """The page with each /static/ asset URL stamped with the file's modification time
    (app.js?v=1727552391), so a browser that cached an older copy fetches the new one."""
    html = page.read_text()
    for name in {m for m in re.findall(r'/static/([\w.\-]+\.(?:js|css))', html)}:
        f = STATIC / name
        if f.exists():
            html = html.replace(f"/static/{name}\"", f"/static/{name}?v={int(f.stat().st_mtime)}\"")
    return html


class FreshStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response
MAX_SPEND = float(os.environ.get("SHARPBET_MAX_SPEND", "1.00"))


class ChatIn(BaseModel):
    session_id: str = Field(min_length=8, max_length=64)
    message: str = Field(min_length=1, max_length=4000)


class SessionIn(BaseModel):
    session_id: str = Field(min_length=8, max_length=64)


def _payload(r, s) -> dict:
    return {"text": r.text, "slip": r.slip_data, "slip_text": r.slip, "untraced": r.untraced,
            "cost": round(r.cost, 4), "total_cost": round(r.total_cost, 4), "max_spend": s.max_spend}


def _sse(ev: dict) -> str:
    return f"data: {json.dumps(ev)}\n\n"


def create_app(session_factory: Callable[[], ChatSession] | None = None) -> FastAPI:
    load_env()
    factory = session_factory or (lambda: ChatSession(max_spend=MAX_SPEND))
    sessions: dict[str, ChatSession] = {}
    locks: dict[str, threading.Lock] = {}
    stops: dict[str, threading.Event] = {}
    # What the page shows, per conversation, so a refresh can redraw it (in memory for now; a server
    # restart clears it, like the conversation itself).
    transcripts: dict[str, list[dict]] = {}
    app = FastAPI(title="sharpbet", docs_url=None, redoc_url=None)

    def get(sid: str) -> ChatSession:
        if sid not in sessions:
            sessions[sid] = factory()
            locks[sid] = threading.Lock()
            stops[sid] = threading.Event()
            transcripts[sid] = []
        return sessions[sid]

    @app.post("/api/chat")
    def chat(body: ChatIn):
        missing = [k for k in ("ANTHROPIC_API_KEY", "ODDS_API_KEY") if not os.environ.get(k)]
        if missing and session_factory is None:
            return {"error": f"Missing {', '.join(missing)} in .env (see .env.example)."}
        s = get(body.session_id)
        with locks[body.session_id]:  # one message at a time per conversation
            try:
                r = s.send(body.message)
            except SpendCapReached as e:
                return {"error": f"{e} Start a new chat, or raise SHARPBET_MAX_SPEND.", "cap": True,
                        "total_cost": s.total_cost}
            except Exception as e:  # show it in the chat instead of a blank failure
                return {"error": f"Something went wrong: {e}"}
        return _payload(r, s)

    @app.post("/api/chat/stream")
    async def chat_stream(body: ChatIn, request: Request):
        """Server-sent events: status, text (deltas), then done / stopped / error. If the page
        disconnects (the user pressed stop), the turn stops and is rolled back."""
        missing = [k for k in ("ANTHROPIC_API_KEY", "ODDS_API_KEY") if not os.environ.get(k)]
        if missing and session_factory is None:
            async def err():
                yield _sse({"type": "error", "error": f"Missing {', '.join(missing)} in .env (see .env.example)."})
            return StreamingResponse(err(), media_type="text/event-stream")
        s = get(body.session_id)
        stop = stops[body.session_id]
        events: queue.Queue = queue.Queue()

        log = transcripts[body.session_id]

        def work():
            with locks[body.session_id]:
                stop.clear()
                log.append({"role": "user", "text": body.message})
                said = []
                on_event = lambda ev: (said.append(ev["delta"]) if ev["type"] == "text" else None, events.put(ev))
                try:
                    r = s.send(body.message, on_event=on_event, should_stop=stop.is_set)
                    done = {"type": "done", **_payload(r, s)}
                    log.append({"role": "bot", **done})
                    events.put(done)
                except Stopped:
                    log.append({"role": "bot", "type": "stopped", "text": "".join(said)})
                    events.put({"type": "stopped"})
                except SpendCapReached as e:
                    err = {"type": "error", "cap": True, "total_cost": s.total_cost,
                           "error": f"{e} Start a new chat, or raise SHARPBET_MAX_SPEND."}
                    log.append({"role": "bot", **err})
                    events.put(err)
                except Exception as e:
                    err = {"type": "error", "error": f"Something went wrong: {e}"}
                    log.append({"role": "bot", **err})
                    events.put(err)

        threading.Thread(target=work, daemon=True).start()

        async def gen():
            loop = asyncio.get_running_loop()
            try:
                while True:
                    if await request.is_disconnected():
                        stop.set()
                        return
                    try:
                        ev = await loop.run_in_executor(None, events.get, True, 0.25)
                    except queue.Empty:
                        continue
                    yield _sse(ev)
                    if ev["type"] in ("done", "stopped", "error"):
                        return
            except asyncio.CancelledError:  # client went away mid-stream
                stop.set()
                raise

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/stop")
    def stop_turn(body: SessionIn):
        if body.session_id in stops:
            stops[body.session_id].set()
        return {"ok": True}

    @app.post("/api/reset")
    def reset(body: SessionIn):
        if body.session_id in sessions:
            sessions[body.session_id].reset()
            transcripts[body.session_id].clear()
        return {"ok": True}

    @app.get("/api/history")
    def history(session_id: str):
        s = sessions.get(session_id)
        return {"messages": transcripts.get(session_id, []),
                "total_cost": round(s.total_cost, 4) if s else 0.0}

    @app.get("/api/status")
    def status():
        from sharpbet.assistant import tools
        return {"keys_ok": all(os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "ODDS_API_KEY")),
                "odds_credits_left": getattr(tools._API, "remaining", None), "max_spend": MAX_SPEND}

    app.mount("/static", FreshStaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def landing():
        return HTMLResponse(versioned(STATIC / "landing.html"), headers=NO_CACHE)

    @app.get("/chat")
    def chat_page():
        return HTMLResponse(versioned(STATIC / "chat.html"), headers=NO_CACHE)

    return app


if os.environ.get("SHARPBET_DEMO"):
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from demo import demo_session
    app = create_app(demo_session)
else:
    app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))
