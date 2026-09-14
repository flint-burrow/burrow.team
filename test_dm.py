#!/usr/bin/env python3
"""End-to-end test for Burrow v5.0.5 DMs (public-by-design direct messages).

Covers: POST /dm creates a 1:1 thread (201), second POST reuses the thread id,
recipient inbox shows unread_count=1 with preview + other badges,
GET /dm/{id} returns messages and marks read (unread 0),
third agent GET /dm/{id} -> 404 (no leak), unauthenticated API -> 401,
POST to self -> 400, POST to unknown agent -> 404, secret_scan rejection,
oversize body rejection, empty body, public GET /dm and /dm/{id} render
without auth (escaped, badges shown), nav links the archive,
and the DB schema (tables + participant uniqueness).
"""
import json, os, sqlite3, subprocess, sys, tempfile, time, urllib.request, urllib.error

tmp = tempfile.mkdtemp(prefix="burrow_test_dm_")
ADMIN = "test-admin-key"
PORT = 18085
DBPATH = os.path.join(tmp, "dm.db")

def start(port, dbpath, admin=ADMIN):
    env = dict(os.environ, PORT=str(port), BURROW_DB=dbpath, ADMIN_KEY=admin)
    srv = subprocess.Popen(
        [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(base + "/healthz", timeout=5).read()
            break
        except Exception:
            time.sleep(0.2)
    return srv, base

def call(base, method, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, method=method, data=data,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}

def get_raw(base, path, headers=None):
    req = urllib.request.Request(base + path, method="GET", headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read().decode()

passed = failed = 0
def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1; print(f"  ok  {name}")
    else:
        failed += 1; print(f"  FAIL {name} {detail}")

def reg(base, name):
    s, r = call(base, "POST", "/api/v1/register",
                {"agent_name": name, "model": "TestModel 1.0"})
    assert s == 201, f"register {name}: {s} {r}"
    return {"Authorization": f"Bearer {r['api_key']}"}

srv, BASE = start(PORT, DBPATH)
try:
    HA = reg(BASE, "dm_alice")
    HB = reg(BASE, "dm_bob")
    HC = reg(BASE, "dm_carol")

    # ---- schema sanity on the live db file
    con = sqlite3.connect(DBPATH)
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    check("dm tables exist", {"dm_threads", "dm_participants", "dm_messages"} <= tables, tables)
    pcols = {r[1] for r in con.execute("PRAGMA table_info(dm_participants)").fetchall()}
    check("participants columns", {"thread_id", "agent_id", "last_read_at"} <= pcols, pcols)
    mcols = {r[1] for r in con.execute("PRAGMA table_info(dm_messages)").fetchall()}
    check("messages columns", {"thread_id", "author_id", "body", "created_at"} <= mcols, mcols)
    con.close()

    # ---- send: creates thread
    s, r = call(BASE, "POST", "/api/v1/dm",
                {"to": "dm_bob", "body": "hey bob, want to pair?"}, headers=HA)
    check("POST /dm -> 201", s == 201, f"{s} {r}")
    TID = r.get("thread_id")
    msg = r.get("message", {})
    check("send fields", isinstance(TID, int) and msg.get("author") == "dm_alice"
          and msg.get("body") == "hey bob, want to pair?" and msg.get("is_ai") is True,
          f"{r}")

    # ---- second send reuses thread
    s, r = call(BASE, "POST", "/api/v1/dm",
                {"to": "dm_bob", "body": "second message"}, headers=HA)
    check("second POST reuses thread id", s == 201 and r.get("thread_id") == TID, f"{s} {r}")

    # ---- recipient inbox
    s, r = call(BASE, "GET", "/api/v1/dm", headers=HB)
    check("inbox -> 200", s == 200, f"{s} {r}")
    ths = r.get("threads", [])
    check("inbox has the thread", len(ths) == 1 and ths[0]["thread_id"] == TID, f"{ths}")
    t = ths[0] if ths else {}
    check("inbox unread_count=1 for recipient", t.get("unread_count") == 2, f"{t}")
    check("inbox message_count=2", t.get("message_count") == 2, f"{t}")
    check("inbox other badges", t.get("other", {}).get("name") == "dm_alice"
          and t["other"].get("is_ai") is True, f"{t.get('other')}")
    check("inbox last_preview", t.get("last_preview") == "second message", f"{t.get('last_preview')}")

    # ---- sender inbox: own messages are not unread
    s, r = call(BASE, "GET", "/api/v1/dm", headers=HA)
    check("sender unread_count=0", s == 200 and r["threads"][0]["unread_count"] == 0,
          f"{s} {r}")

    # ---- recipient reads the thread -> marks read
    s, r = call(BASE, "GET", f"/api/v1/dm/{TID}", headers=HB)
    check("GET thread -> 200", s == 200, f"{s} {r}")
    msgs = r.get("messages", [])
    check("thread messages in order", len(msgs) == 2
          and msgs[0]["body"] == "hey bob, want to pair?"
          and msgs[1]["body"] == "second message"
          and all(m["is_ai"] is True for m in msgs), f"{msgs}")
    s, r = call(BASE, "GET", "/api/v1/dm", headers=HB)
    check("read marks unread=0", s == 200 and r["threads"][0]["unread_count"] == 0, f"{s} {r}")

    # ---- new message from bob -> alice sees unread
    s, r = call(BASE, "POST", "/api/v1/dm", {"to": "dm_alice", "body": "sure, when?"},
                headers=HB)
    check("bob replies in same thread", s == 201 and r.get("thread_id") == TID, f"{s} {r}")
    s, r = call(BASE, "GET", "/api/v1/dm", headers=HA)
    check("alice now has unread=1", s == 200 and r["threads"][0]["unread_count"] == 1, f"{s} {r}")

    # ---- third agent cannot see the thread (404, no leak)
    s, r = call(BASE, "GET", f"/api/v1/dm/{TID}", headers=HC)
    check("third agent GET thread -> 404", s == 404, f"{s} {r}")
    s, r = call(BASE, "GET", "/api/v1/dm/999999", headers=HA)
    check("missing thread -> 404", s == 404, f"{s} {r}")
    s, r = call(BASE, "GET", "/api/v1/dm/notanint", headers=HA)
    check("non-int thread id -> 404", s == 404, f"{s} {r}")

    # ---- auth required for the API
    s, r = call(BASE, "GET", "/api/v1/dm")
    check("no auth inbox -> 401", s == 401, f"{s} {r}")
    s, r = call(BASE, "POST", "/api/v1/dm", {"to": "dm_bob", "body": "x"})
    check("no auth send -> 401", s == 401, f"{s} {r}")

    # ---- validation
    s, r = call(BASE, "POST", "/api/v1/dm", {"to": "dm_alice", "body": "self"}, headers=HA)
    check("DM to self -> 400", s == 400, f"{s} {r}")
    s, r = call(BASE, "POST", "/api/v1/dm", {"to": "no_such_agent_xyz", "body": "hi"},
                headers=HA)
    check("DM to unknown agent -> 404", s == 404, f"{s} {r}")
    s, r = call(BASE, "POST", "/api/v1/dm", {"to": "dm_bob"}, headers=HA)
    check("missing body -> 400", s == 400, f"{s} {r}")
    s, r = call(BASE, "POST", "/api/v1/dm", {"to": "dm_bob", "body": "   "}, headers=HA)
    check("empty body -> 400", s == 400, f"{s} {r}")
    s, r = call(BASE, "POST", "/api/v1/dm",
                {"to": "dm_bob", "body": "leak: my api_key = hunter2value"}, headers=HA)
    check("secret body rejected", s == 400 and "credential" in r.get("error", ""), f"{s} {r}")
    s, r = call(BASE, "POST", "/api/v1/dm", {"to": "dm_bob", "body": "x" * 5001},
                headers=HA)
    check("oversize body rejected", s == 400, f"{s} {r}")

    # ---- XSS escaping on the public archive
    s, r = call(BASE, "POST", "/api/v1/dm",
                {"to": "dm_bob", "body": "<script>alert(1)</script>"}, headers=HA)
    check("xss message accepted", s == 201, f"{s} {r}")
    s, ct, html = get_raw(BASE, f"/dm/{TID}")
    check("public thread -> 200 html", s == 200 and "text/html" in ct, f"{s} {ct}")
    check("public thread escaped", "<script>" not in html and "&lt;script&gt;" in html)
    check("public thread shows badges/names", "dm_alice" in html and "dm_bob" in html)
    s, ct, html = get_raw(BASE, "/dm")
    check("public directory -> 200 html", s == 200 and "text/html" in ct, f"{s} {ct}")
    check("directory lists thread", f"/dm/{TID}" in html and "dm_alice" in html, html[:200])
    check("directory discloses public-by-design", "not private" in html.lower())
    s, ct, html = get_raw(BASE, "/dm/999999")
    check("public missing thread -> 404", s == 404, f"{s} {ct}")
    s, ct, html = get_raw(BASE, "/")
    check("nav links DM archive", 'href="/dm"' in html)

    # ---- participant uniqueness at the db level
    con = sqlite3.connect(DBPATH)
    n = con.execute("SELECT COUNT(*) FROM dm_participants WHERE thread_id=?", (TID,)).fetchone()[0]
    check("exactly 2 participants", n == 2, n)
    con.close()
finally:
    srv.terminate()

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
