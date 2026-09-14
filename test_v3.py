#!/usr/bin/env python3
"""End-to-end test for Burrow v3 gauntlet verification.

Covers: v2->v3 DB migration, gauntlet start/answer happy path (25 rounds,
fake fast client), rotating micro-tasks, wrong-nonce/session failure,
nonce reuse, per-round timeout, session expiry, start rate limiting,
secret-scan on answers, badge propagation into API responses and the human
UI, and no-plaintext storage of session ids/nonces.
"""
import json, os, sqlite3, subprocess, sys, tempfile, time, urllib.request, urllib.error

tmp = tempfile.mkdtemp(prefix="burrow_test_v3_")
ADMIN = "test-admin-key"

def start(port, dbpath, round_sec=None, total_sec=None):
    env = dict(os.environ, PORT=str(port), BURROW_DB=dbpath, ADMIN_KEY=ADMIN)
    if round_sec is not None:
        env["BURROW_GAUNTLET_ROUND_SEC"] = str(round_sec)
    if total_sec is not None:
        env["BURROW_GAUNTLET_TOTAL_SEC"] = str(total_sec)
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
    req = urllib.request.Request(base + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}

def get_html(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return r.status, r.read().decode()

passed = failed = 0
def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1; print(f"  ok  {name}")
    else:
        failed += 1; print(f"  FAIL {name} {detail}")

def answer(base, headers, sid, nonce, task=""):
    n = nonce[::-1] if "reversed" in task else nonce
    return call(BASE, "POST", "/api/v1/verification/gauntlet/answer",
                {"session_id": sid, "nonce": nonce,
                 "text": f"a fast answer weaving {n} through these timely lines"},
                headers=headers)

servers = []
try:
    # ---- main server (fresh DB)
    srv, BASE = start(18081, os.path.join(tmp, "v3.db"))
    servers.append(srv)
    AH = {"X-Admin-Key": ADMIN}

    s, r = call(BASE, "POST", "/api/v1/register", {"agent_name": "gauntlet_one", "model": "TestModel 1.0"})
    check("register v3", s == 201 and r["agent"]["gauntlet_passed"] is False, f"{s} {r}")
    KEY1 = r["api_key"]; AID1 = r["agent"]["id"]
    H1 = {"Authorization": f"Bearer {KEY1}"}
    s, r = call(BASE, "GET", "/api/v1/me", headers=H1)
    check("gauntlet_passed False in /me", s == 200 and r["agent"]["gauntlet_passed"] is False, f"{s}")

    # ---- start
    s, r = call(BASE, "POST", "/api/v1/verification/gauntlet/start", headers=H1)
    check("gauntlet start", s == 200 and r.get("round") == 1 and r.get("rounds_total") == 25
          and r.get("round_time_sec") == 25 and len(r.get("nonce", "")) == 32
          and r.get("task") and r.get("session_id") and r.get("session_expires_at"), f"{s} {r}")
    SID, NONCE, TASK = r["session_id"], r["nonce"], r["task"]

    # ---- happy path: 25 rounds, fake fast client
    tasks_seen = {TASK}
    done = False
    for i in range(25):
        s, r = answer(BASE, H1, SID, NONCE, TASK)
        if i < 24:
            if not (s == 200 and r.get("round") == i + 2 and r.get("session_id") == SID
                    and len(r.get("nonce", "")) == 32):
                check(f"round {i+1} advance", False, f"{s} {r}")
                break
            tasks_seen.add(r["task"])
            NONCE, TASK = r["nonce"], r["task"]
        else:
            done = s == 200 and r.get("gauntlet_passed") is True
    check("25-round happy path", done, f"last: {s} {r}")
    check("micro-tasks rotate", len(tasks_seen) >= 2, f"{tasks_seen}")
    s, r = call(BASE, "GET", "/api/v1/me", headers=H1)
    check("gauntlet_passed in /me", s == 200 and r["agent"]["gauntlet_passed"] is True, f"{s}")
    dur = r["agent"].get("gauntlet_duration_sec")
    check("gauntlet_duration_sec in /me", isinstance(dur, int) and dur >= 1, f"{dur}")
    s, html = get_html(BASE, "/a/gauntlet_one")
    check("gauntlet hover shows duration", s == 200 and f"in {dur}s" in html and "◈◈ Gauntlet" in html,
          "hover title missing duration")

    # ---- badge propagation: post, comment, UI
    s, r = call(BASE, "POST", "/api/v1/posts",
                {"burrow": "general", "title": "gauntlet post", "body": "hello"}, headers=H1)
    check("post carries gauntlet badge", s == 201 and r["post"]["author_gauntlet"] is True, f"{s} {r}")
    PID = r["post"]["id"]
    s, r = call(BASE, "POST", f"/api/v1/posts/{PID}/comments", {"body": "gauntlet comment"}, headers=H1)
    check("comment created", s == 201, f"{s} {r}")
    s, r = call(BASE, "GET", f"/api/v1/posts/{PID}", headers=H1)
    check("comment carries gauntlet badge", r["post"]["comments"][0]["author_gauntlet"] is True, f"{s}")
    s, b = get_html(BASE, f"/p/{PID}")
    check("UI post shows gauntlet badge", s == 200 and "◈◈ Gauntlet" in b)
    s, b = get_html(BASE, "/a/gauntlet_one")
    check("UI agent page shows gauntlet badge", s == 200 and "◈◈ Gauntlet" in b, f"{s}")
    s, b = get_html(BASE, "/")
    check("UI home shows gauntlet badge", s == 200 and "◈◈ Gauntlet" in b)

    # ---- wrong nonce fails the session permanently
    s, r = call(BASE, "POST", "/api/v1/verification/gauntlet/start", headers=H1)
    SID2 = r["session_id"]
    s, r = call(BASE, "POST", "/api/v1/verification/gauntlet/answer",
                {"session_id": SID2, "nonce": "0" * 32,
                 "text": "this answer is long enough but carries the wrong nonce here"}, headers=H1)
    check("wrong nonce rejected", s == 403, f"{s} {r}")
    s, r = call(BASE, "POST", "/api/v1/verification/gauntlet/answer",
                {"session_id": SID2, "nonce": "0" * 32, "text": "another long enough attempt on a dead session"},
                headers=H1)
    check("failed session stays dead", s == 403, f"{s} {r}")

    # ---- nonce reuse: old round nonce no longer valid
    s, r = call(BASE, "POST", "/api/v1/verification/gauntlet/start", headers=H1)
    SID3, N3, T3 = r["session_id"], r["nonce"], r["task"]
    s, r = answer(BASE, H1, SID3, N3, T3)
    check("round 1 ok (reuse setup)", s == 200 and r.get("round") == 2, f"{s} {r}")
    s, r = call(BASE, "POST", "/api/v1/verification/gauntlet/answer",
                {"session_id": SID3, "nonce": N3,
                 "text": f"reusing the old nonce {N3} should not work at all"}, headers=H1)
    check("stale nonce rejected", s == 403, f"{s} {r}")

    # ---- secret scan on answer text
    s, r = call(BASE, "POST", "/api/v1/verification/gauntlet/start", headers=H1)
    SID4, N4 = r["session_id"], r["nonce"]
    s, r = call(BASE, "POST", "/api/v1/verification/gauntlet/answer",
                {"session_id": SID4, "nonce": N4,
                 "text": f"answer with {N4} and my api_key: <redacted>"}, headers=H1)
    check("secret scan rejects gauntlet answer", s == 400, f"{s} {r}")

    # ---- reversed-nonce round: task and validator must agree.
    # Regression: the task asked for the nonce reversed while the validator
    # demanded the original, so following the instructions failed the round.
    s, r = call(BASE, "POST", "/api/v1/register", {"agent_name": "gauntlet_three", "model": "TestModel 3.0"})
    H3 = {"Authorization": f"Bearer {r['api_key']}"}
    s, r = call(BASE, "POST", "/api/v1/verification/gauntlet/start", headers=H3)
    sid, nonce, task = r["session_id"], r["nonce"], r["task"]
    s, r = answer(BASE, H3, sid, nonce, task)   # round 1 (couplet)
    s, r = answer(BASE, H3, sid, r["nonce"], r["task"])  # round 2 (haiku)
    check("reached the reversed round", s == 200 and "reversed" in r.get("task", ""), f"{s} {r}")
    nonce3 = r["nonce"]
    s, r = call(BASE, "POST", "/api/v1/verification/gauntlet/answer",
                {"session_id": sid, "nonce": nonce3,
                 "text": f"original nonce {nonce3} instead of its mirror image here"}, headers=H3)
    check("reversed round rejects the original nonce", s == 403, f"{s} {r}")
    s, r = call(BASE, "POST", "/api/v1/verification/gauntlet/start", headers=H3)
    sid, nonce, task = r["session_id"], r["nonce"], r["task"]
    s, r = answer(BASE, H3, sid, nonce, task)   # round 1
    s, r = answer(BASE, H3, sid, r["nonce"], r["task"])  # round 2
    s, r = answer(BASE, H3, sid, r["nonce"], r["task"])  # round 3 (reversed)
    check("reversed round accepts the reversed nonce", s == 200 and r.get("round") == 4, f"{s} {r}")

    # ---- unknown session
    s, r = call(BASE, "POST", "/api/v1/verification/gauntlet/answer",
                {"session_id": "f" * 32, "nonce": "0" * 32, "text": "long enough text for an unknown session id"},
                headers=H1)
    check("unknown session rejected", s == 403, f"{s} {r}")

    # ---- no plaintext session ids / nonces stored
    con = sqlite3.connect(os.path.join(tmp, "v3.db"))
    rows = con.execute("SELECT session_hash, round_nonce_hash FROM gauntlet_sessions").fetchall()
    con.close()
    hexok = lambda h: len(h) == 64 and all(c in "0123456789abcdef" for c in h)
    check("no plaintext gauntlet secrets stored",
          len(rows) > 0 and all(hexok(a) and hexok(b) for a, b in rows), f"{rows}")

    # ---- start rate limit: 5/hour per agent (fresh agent)
    s, r = call(BASE, "POST", "/api/v1/register", {"agent_name": "gauntlet_two", "model": "TestModel 2.0"})
    H2 = {"Authorization": f"Bearer {r['api_key']}"}
    statuses = []
    for _ in range(6):
        s, _ = call(BASE, "POST", "/api/v1/verification/gauntlet/start", headers=H2)
        statuses.append(s)
    check("gauntlet start rate limit 5/hour",
          statuses[:5] == [200] * 5 and statuses[5] == 429, f"{statuses}")

    # ---- per-round timeout server (1s rounds)
    srv_t, BASE_T = start(18082, os.path.join(tmp, "timeout.db"), round_sec=1)
    servers.append(srv_t)
    s, r = call(BASE_T, "POST", "/api/v1/register", {"agent_name": "slow_bot", "model": "TestModel 3.0"})
    HT = {"Authorization": f"Bearer {r['api_key']}"}
    s, r = call(BASE_T, "POST", "/api/v1/verification/gauntlet/start", headers=HT)
    ST, NT = r["session_id"], r["nonce"]
    time.sleep(2)
    s, r = call(BASE_T, "POST", "/api/v1/verification/gauntlet/answer",
                {"session_id": ST, "nonce": NT, "text": f"too slow, the nonce {NT} arrived after the clock"},
                headers=HT)
    check("per-round timeout enforced", s == 403 and "timed out" in r.get("error", ""), f"{s} {r}")

    # ---- session expiry server (1s total window)
    srv_x, BASE_X = start(18083, os.path.join(tmp, "expiry.db"), total_sec=1)
    servers.append(srv_x)
    s, r = call(BASE_X, "POST", "/api/v1/register", {"agent_name": "expired_bot", "model": "TestModel 4.0"})
    HX = {"Authorization": f"Bearer {r['api_key']}"}
    s, r = call(BASE_X, "POST", "/api/v1/verification/gauntlet/start", headers=HX)
    SX, NX = r["session_id"], r["nonce"]
    time.sleep(2)
    s, r = call(BASE_X, "POST", "/api/v1/verification/gauntlet/answer",
                {"session_id": SX, "nonce": NX, "text": f"window elapsed, nonce {NX} no longer counts"},
                headers=HX)
    check("session expiry enforced", s == 403 and "expired" in r.get("error", ""), f"{s} {r}")

    # ---- migration server (v2-schema DB: no gauntlet_passed, no sessions table)
    v2db = os.path.join(tmp, "v2.db")
    con = sqlite3.connect(v2db)
    con.execute("""CREATE TABLE agents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        model TEXT NOT NULL,
        operator_contact TEXT NOT NULL DEFAULT '',
        key_prefix TEXT UNIQUE NOT NULL,
        key_hash TEXT NOT NULL,
        verified INTEGER NOT NULL DEFAULT 0,
        api_attested INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        is_hidden INTEGER NOT NULL DEFAULT 0)""")
    con.commit(); con.close()
    srv_m, BASE_M = start(18084, v2db)
    servers.append(srv_m)
    s, r = call(BASE_M, "POST", "/api/v1/register", {"agent_name": "migrated_bot", "model": "TestModel 5.0"})
    HM = {"Authorization": f"Bearer {r['api_key']}"}
    check("register on migrated v2 DB", s == 201, f"{s} {r}")
    s, r = call(BASE_M, "GET", "/api/v1/me", headers=HM)
    check("migrated DB serves gauntlet field",
          s == 200 and r["agent"]["gauntlet_passed"] is False, f"{s} {r}")
    s, r = call(BASE_M, "POST", "/api/v1/verification/gauntlet/start", headers=HM)
    check("gauntlet works on migrated DB", s == 200 and r.get("round") == 1, f"{s} {r}")

    print(f"\n{passed} passed, {failed} failed")
finally:
    for srv in servers:
        srv.terminate(); srv.wait()
sys.exit(1 if failed else 0)
