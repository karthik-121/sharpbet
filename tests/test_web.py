"""Web server tests, using demo mode (synthetic sample prices, scripted model): no network, no spend."""
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))
from demo import demo_session  # noqa: E402
from server import create_app  # noqa: E402

client = TestClient(create_app(demo_session))
SID = "test-session-0001"


def test_pages_and_assets_load():
    home = client.get("/")
    assert home.status_code == 200 and "locked profit" in home.text and 'href="/chat"' in home.text
    assert "1-800-GAMBLER" in home.text and "guaranteed profit" not in home.text.lower()
    chat = client.get("/chat")
    assert chat.status_code == 200 and "What promo are you working with?" in chat.text and 'href="/"' in chat.text
    for f in ("/static/app.js", "/static/style.css", "/static/landing.css"):
        assert client.get(f).status_code == 200


def test_boost_returns_text_and_structured_slip():
    r = client.post("/api/chat", json={"session_id": SID, "message": "50% boost on draftkings"}).json()
    assert "error" not in r and r["text"].startswith("Demo mode")
    slip = r["slip"]
    assert slip["kind"] == "plan" and slip["sections"][0]["type"] == "boost"
    boosted = slip["sections"][0]["tickets"][0]
    assert boosted["boost"] and boosted["funding"] == "Cash" and boosted["book"] == "DraftKings"
    assert f"${slip['locked_profit']:,.2f}" in r["slip_text"] and r["untraced"] == []


def test_grid_and_partial_placement_in_one_conversation():
    sid = "test-session-grid"
    r = client.post("/api/chat", json={"session_id": sid, "message": "$200 bonus bet on fanduel"}).json()
    sec = r["slip"]["sections"][0]
    assert sec["type"] == "bonus" and len(sec["tickets"]) >= 16      # the most profitable grid
    assert all(len(t["legs"]) == len(sec["tickets"][0]["legs"]) for t in sec["tickets"])
    r = client.post("/api/chat", json={"session_id": sid, "message": "placed 1-4, 5 moved"}).json()
    assert r["slip"]["kind"] == "resolve" and r["slip"]["keep"] == [1, 2, 3, 4]


def test_reset_and_validation():
    assert client.post("/api/reset", json={"session_id": SID}).json() == {"ok": True}
    assert client.post("/api/chat", json={"session_id": "x", "message": "hi"}).status_code == 422
    assert client.post("/api/chat", json={"session_id": SID, "message": ""}).status_code == 422
    assert "keys_ok" in client.get("/api/status").json()


def test_streaming_endpoint_sends_status_text_then_done():
    import json as _json
    with client.stream("POST", "/api/chat/stream",
                       json={"session_id": "test-session-stream", "message": "50% boost on draftkings"}) as r:
        events = [_json.loads(line[6:]) for line in r.iter_lines() if line.startswith("data: ")]
    kinds = [e["type"] for e in events]
    assert kinds[0] == "status" and "text" in kinds and kinds[-1] == "done"
    streamed = "".join(e["delta"] for e in events if e["type"] == "text")
    assert streamed == events[-1]["text"] and events[-1]["slip"]["sections"][0]["type"] == "boost"
    assert client.post("/api/stop", json={"session_id": "test-session-stream"}).json() == {"ok": True}


def test_history_restores_after_refresh_and_clears_on_new_chat():
    sid = "test-session-history"
    with client.stream("POST", "/api/chat/stream", json={"session_id": sid, "message": "50% boost on dk"}) as r:
        list(r.iter_lines())
    h = client.get(f"/api/history?session_id={sid}").json()
    assert [m["role"] for m in h["messages"]] == ["user", "bot"]
    assert h["messages"][0]["text"] == "50% boost on dk" and h["messages"][1]["slip"]["kind"] == "plan"
    client.post("/api/reset", json={"session_id": sid})
    assert client.get(f"/api/history?session_id={sid}").json()["messages"] == []
    assert client.get("/api/history?session_id=unknown-session").json()["messages"] == []


def test_landing_faq_says_beta_and_stays_honest():
    page = client.get("/").text
    assert 'id="faq"' in page and "Is sharpbet in beta?" in page and "More is coming soon" in page
    assert "Is the profit guaranteed?" in page and "This isn't legal advice." in page
    # no unverifiable promises
    assert "guaranteed profit" not in page.lower() and "risk-free" not in page.lower()


def test_offer_comparison_returns_table_and_winner_slip():
    r = client.post("/api/chat", json={"session_id": "test-session-compare",
                                       "message": "two 50% boosts or one 100% boost?"}).json()
    c = r["slip"]["comparison"]
    assert len(c["rows"]) == 2 and c["rows"][0]["best"] and c["verdict"].startswith("Best: ")
    assert r["slip"]["sections"] and "Best: " in r["text"]
    # every option carries its own slip, so clicking a row can show its bets
    for row in c["rows"]:
        assert row["slip"]["sections"] and row["slip"]["locked_profit"] == row["locked_profit"]
    assert c["rows"][1]["slip"]["sections"] != c["rows"][0]["slip"]["sections"]


def test_pages_and_assets_are_never_served_stale():
    for url in ("/", "/chat", "/static/app.js", "/static/style.css"):
        assert client.get(url).headers["cache-control"] == "no-cache"
    # asset URLs change whenever the file does, so an old cached copy can't be reused
    import re
    html = client.get("/chat").text
    assert re.search(r'/static/app\.js\?v=\d+"', html) and re.search(r'/static/style\.css\?v=\d+"', html)
