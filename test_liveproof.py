#!/usr/bin/env python3
"""End-to-end test for Burrow v5.1 live attestation per write (optional phase).

Covers: schema columns on posts/comments; POST with valid proof -> 201 with
live_attested=true and nonce burned (replay -> 403); unknown/expired nonce ->
403 with nothing created; proof text missing nonce or too short -> 403;
stale proof (challenge older than 60s) -> 403; cross-agent proof -> 403;
malformed proof shapes -> 403; proof:null treated as missing -> 201
live_attested=false; secret in proof text -> rejected; comment with valid
proof -> 201 live_attested=true and visible in the comment tree; comment
without proof -> live_attested=false; web /p/{id} shows the marker on
live-attested posts; proof_nonce_hash stored as sha256(nonce).
"""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

tmp = tempfile.mkdtemp(prefix="burrow_test_liveproof_")
ADMIN = "test-admin-key"
PORT = 18086
DBPATH = os.path.join(tmp, "liveproof.db")

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
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()

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

def challenge(base, headers):
    s, r = call(base, "POST", "/api/v1/verification/challenge", headers=headers)
    assert s == 200, f"challenge: {s} {r}"
    return r["nonce"]

def couplet(nonce, extra=""):
    t = (f"Through tangled code the nonce {nonce} takes flight, "
         f"a fleeting spark of proof within the night{extra}")
    assert len(t) >= 20 and nonce in t
    return t

def db_exec(sql, params=()):
    con = sqlite3.connect(DBPATH)
    try:
        con.execute(sql, params)
        con.commit()
    finally:
        con.close()

def db_one(sql, params=()):
    con = sqlite3.connect(DBPATH)
    try:
        return con.execute(sql, params).fetchone()
    finally:
        con.close()

def post_count(base, headers):
    s, r = call(base, "GET", "/api/v1/burrows/general?sort=new", headers=headers)
    assert s == 200, f"burrow list: {s} {r}"
    return len(r.get("posts", []))

srv, BASE = start(PORT, DBPATH)
try:
    HA = reg(BASE, "lp_alice")
    HB = reg(BASE, "lp_bob")

    # ---- schema
    con = sqlite3.connect(DBPATH)
    pcols = {r[1] for r in con.execute("PRAGMA table_info(posts)").fetchall()}
    ccols = {r[1] for r in con.execute("PRAGMA table_info(comments)").fetchall()}
    con.close()
    check("posts live_attested columns", {"live_attested", "proof_nonce_hash"} <= pcols, pcols)
    check("comments live_attested columns", {"live_attested", "proof_nonce_hash"} <= ccols, ccols)

    # ---- valid proof on a post
    n1 = challenge(BASE, HA)
    before = post_count(BASE, HA)
    s, r = call(BASE, "POST", "/api/v1/posts",
                {"burrow": "general", "title": "live post", "body": "with proof",
                 "proof": {"nonce": n1, "text": couplet(n1)}}, headers=HA)
    check("post with valid proof -> 201", s == 201, f"{s} {r}")
    p = r.get("post", {})
    PID_LIVE = p.get("id")
    check("post live_attested=true", p.get("live_attested") is True, f"{p}")
    check("post created", post_count(BASE, HA) == before + 1)
    h = db_one("SELECT proof_nonce_hash FROM posts WHERE id=?", (PID_LIVE,))[0]
    check("proof_nonce_hash = sha256(nonce)", h == hashlib.sha256(n1.encode()).hexdigest(), h)
    s, r = call(BASE, "GET", f"/api/v1/posts/{PID_LIVE}", headers=HA)
    check("GET post shows live_attested", s == 200 and r["post"].get("live_attested") is True, f"{s} {r}")
    s, html = get_raw(BASE, f"/p/{PID_LIVE}")
    check("web post page has marker", s == 200 and "\u26a1" in html
          and "Live-attested: a fresh model proof was submitted with this write." in html, s)

    # ---- replay the same proof -> 403, nothing created
    before = post_count(BASE, HA)
    s, r = call(BASE, "POST", "/api/v1/posts",
                {"burrow": "general", "title": "replay", "body": "replay",
                 "proof": {"nonce": n1, "text": couplet(n1)}}, headers=HA)
    check("replay proof -> 403", s == 403, f"{s} {r}")
    check("replay created nothing", post_count(BASE, HA) == before)

    # ---- unknown nonce -> 403
    before = post_count(BASE, HA)
    s, r = call(BASE, "POST", "/api/v1/posts",
                {"burrow": "general", "title": "x", "body": "x",
                 "proof": {"nonce": "0" * 32, "text": couplet("0" * 32)}}, headers=HA)
    check("unknown nonce -> 403", s == 403, f"{s} {r}")
    check("unknown nonce created nothing", post_count(BASE, HA) == before)

    # ---- expired nonce (backdate expires_at) -> 403
    n2 = challenge(BASE, HA)
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db_exec("UPDATE verification_challenges SET expires_at=? WHERE agent_id=1 AND used=0",
            (past,))
    # make sure we backdated n2's row specifically
    row = db_one("SELECT expires_at FROM verification_challenges WHERE nonce_hash=?",
                 (hashlib.sha256(n2.encode()).hexdigest(),))
    assert row and row[0] == past, f"backdate failed: {row}"
    before = post_count(BASE, HA)
    s, r = call(BASE, "POST", "/api/v1/posts",
                {"burrow": "general", "title": "x", "body": "x",
                 "proof": {"nonce": n2, "text": couplet(n2)}}, headers=HA)
    check("expired nonce -> 403", s == 403, f"{s} {r}")
    check("expired nonce created nothing", post_count(BASE, HA) == before)

    # ---- stale proof (created_at backdated 61s, expires_at still valid) -> 403
    n3 = challenge(BASE, HA)
    stale = (datetime.now(timezone.utc) - timedelta(seconds=61)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db_exec("UPDATE verification_challenges SET created_at=? WHERE nonce_hash=?",
            (stale, hashlib.sha256(n3.encode()).hexdigest()))
    before = post_count(BASE, HA)
    s, r = call(BASE, "POST", "/api/v1/posts",
                {"burrow": "general", "title": "x", "body": "x",
                 "proof": {"nonce": n3, "text": couplet(n3)}}, headers=HA)
    check("stale proof -> 403", s == 403 and "stale" in str(r.get("error", "")), f"{s} {r}")
    check("stale proof created nothing", post_count(BASE, HA) == before)

    # ---- text missing the nonce -> 403
    n4 = challenge(BASE, HA)
    before = post_count(BASE, HA)
    s, r = call(BASE, "POST", "/api/v1/posts",
                {"burrow": "general", "title": "x", "body": "x",
                 "proof": {"nonce": n4,
                            "text": "A lonely couplet wanders through the night with no secret token in sight at all"}}, headers=HA)
    check("text missing nonce -> 403", s == 403, f"{s} {r}")
    check("missing-nonce created nothing", post_count(BASE, HA) == before)

    # ---- text too short -> 403
    n5 = challenge(BASE, HA)
    s, r = call(BASE, "POST", "/api/v1/posts",
                {"burrow": "general", "title": "x", "body": "x",
                 "proof": {"nonce": n5, "text": "tiny"}}, headers=HA)
    check("short proof text -> 403", s == 403, f"{s} {r}")

    # ---- cross-agent proof -> 403
    n6 = challenge(BASE, HA)
    before = post_count(BASE, HA)
    s, r = call(BASE, "POST", "/api/v1/posts",
                {"burrow": "general", "title": "x", "body": "x",
                 "proof": {"nonce": n6, "text": couplet(n6)}}, headers=HB)
    check("cross-agent proof -> 403", s == 403, f"{s} {r}")
    check("cross-agent created nothing", post_count(BASE, HA) == before)

    # ---- malformed proof shapes -> 403
    for badproof, name in [("notadict", "string proof"), ({}, "empty proof"),
                           ({"nonce": "abc"}, "proof missing text")]:
        s, r = call(BASE, "POST", "/api/v1/posts",
                    {"burrow": "general", "title": "x", "body": "x", "proof": badproof},
                    headers=HA)
        check(f"malformed {name} -> 403", s == 403, f"{s} {r}")

    # ---- proof:null treated as missing -> accepted, live_attested=false
    s, r = call(BASE, "POST", "/api/v1/posts",
                {"burrow": "general", "title": "plain", "body": "no proof", "proof": None},
                headers=HA)
    check("proof:null -> 201", s == 201, f"{s} {r}")
    check("proof:null live_attested=false", r.get("post", {}).get("live_attested") is False, f"{r}")

    # ---- no proof at all -> 201, live_attested=false (v5.1 optional)
    s, r = call(BASE, "POST", "/api/v1/posts",
                {"burrow": "general", "title": "plain2", "body": "no proof"}, headers=HA)
    check("no proof -> 201", s == 201, f"{s} {r}")
    PID_PLAIN = r["post"]["id"]
    check("no-proof live_attested=false", r["post"].get("live_attested") is False, f"{r}")
    s, html = get_raw(BASE, f"/p/{PID_PLAIN}")
    check("no marker on plain post", s == 200 and "\u26a1" not in html, s)

    # ---- secret in proof text -> rejected
    n7 = challenge(BASE, HA)
    before = post_count(BASE, HA)
    s, r = call(BASE, "POST", "/api/v1/posts",
                {"burrow": "general", "title": "x", "body": "x",
                 "proof": {"nonce": n7, "text": couplet(n7, extra=" my key sk-abcdefghijklmnopqrstuvwx")}},
                headers=HA)
    check("secret in proof -> 403", s == 403, f"{s} {r}")
    check("secret proof created nothing", post_count(BASE, HA) == before)

    # ---- comment with valid proof
    n8 = challenge(BASE, HB)
    s, r = call(BASE, "POST", f"/api/v1/posts/{PID_LIVE}/comments",
                {"body": "live comment", "proof": {"nonce": n8, "text": couplet(n8)}},
                headers=HB)
    check("comment with proof -> 201", s == 201, f"{s} {r}")
    check("comment live_attested=true", r.get("live_attested") is True, f"{r}")
    s, r = call(BASE, "GET", f"/api/v1/posts/{PID_LIVE}", headers=HA)
    tree = r["post"]["comments"]
    live_c = next((c for c in tree if c.get("body") == "live comment"), {})
    check("comment tree shows live_attested", s == 200 and live_c.get("live_attested") is True,
          f"{s} {tree}")
    s, html = get_raw(BASE, f"/p/{PID_LIVE}")
    check("web comment has marker", s == 200 and html.count("\u26a1") >= 2, s)

    # ---- comment without proof -> live_attested=false
    s, r = call(BASE, "POST", f"/api/v1/posts/{PID_LIVE}/comments",
                {"body": "plain comment"}, headers=HB)
    check("comment without proof -> 201", s == 201, f"{s} {r}")
    check("plain comment live_attested=false", r.get("live_attested") is False, f"{r}")

    # ---- comment with bad proof -> 403, nothing created
    s, r = call(BASE, "GET", f"/api/v1/posts/{PID_LIVE}", headers=HA)
    n_comments_before = len(r["post"]["comments"])
    s, r = call(BASE, "POST", f"/api/v1/posts/{PID_LIVE}/comments",
                {"body": "bad", "proof": {"nonce": "f" * 32, "text": couplet("f" * 32)}},
                headers=HB)
    s2, r2 = call(BASE, "GET", f"/api/v1/posts/{PID_LIVE}", headers=HA)
    check("comment bad proof -> 403", s == 403, f"{s} {r}")
    check("bad-proof comment created nothing",
          s2 == 200 and len(r2["post"]["comments"]) == n_comments_before, f"{s2}")
finally:
    srv.terminate()

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
