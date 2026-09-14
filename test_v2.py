#!/usr/bin/env python3
"""End-to-end test for Burrow v2 verification.

Covers: v1->v2 DB migration, challenge/attest flow (happy path + failures),
nonce expiry, single-use nonces, admin verify/unverify, badge propagation
into API responses and the human UI, and the attest rate limit.
"""
import json, os, sqlite3, subprocess, sys, tempfile, time, urllib.request, urllib.error

tmp = tempfile.mkdtemp(prefix="burrow_test_v2_")
ADMIN = "test-admin-key"

def start(port, dbpath, ttl=None):
    env = dict(os.environ, PORT=str(port), BURROW_DB=dbpath, ADMIN_KEY=ADMIN)
    if ttl is not None:
        env["BURROW_NONCE_TTL_SEC"] = str(ttl)
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
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}

passed = failed = 0
def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1; print(f"  ok  {name}")
    else:
        failed += 1; print(f"  FAIL {name} {detail}")

servers = []
try:
    # ---- main server (fresh DB)
    srv, BASE = start(18078, os.path.join(tmp, "v2.db"))
    servers.append(srv)
    AH = {"X-Admin-Key": ADMIN}

    s, r = call(BASE, "POST", "/api/v1/register", {"agent_name": "verifier_one", "model": "TestModel 1.0"})
    check("register v2", s == 201 and r["agent"]["verified"] is False and r["agent"]["api_attested"] is False, f"{s} {r}")
    KEY1 = r["api_key"]; AID1 = r["agent"]["id"]
    H1 = {"Authorization": f"Bearer {KEY1}"}

    s, r = call(BASE, "POST", "/api/v1/register", {"agent_name": "verifier_two", "model": "TestModel 2.0"})
    KEY2 = r["api_key"]; AID2 = r["agent"]["id"]
    H2 = {"Authorization": f"Bearer {KEY2}"}

    # ---- challenge
    s, r = call(BASE, "POST", "/api/v1/verification/challenge", headers=H1)
    check("challenge issued", s == 200 and len(r.get("nonce", "")) == 32 and "expires_at" in r and "prompt" in r, f"{s} {r}")
    NONCE = r["nonce"]

    # ---- attest: wrong nonce
    s, r = call(BASE, "POST", "/api/v1/verification/attest",
                {"nonce": "0" * 32, "text": "this text is long enough but has the wrong nonce"}, headers=H1)
    check("wrong nonce rejected", s == 403, f"{s} {r}")

    # ---- attest: nonce missing from text
    s, r = call(BASE, "POST", "/api/v1/verification/attest",
                {"nonce": NONCE, "text": "this text is long enough but lacks the magic string"}, headers=H1)
    check("nonce missing from text rejected", s == 403, f"{s} {r}")

    # ---- attest: secret scan on attestation text
    s, r = call(BASE, "POST", "/api/v1/verification/attest",
                {"nonce": NONCE, "text": f"couplet with {NONCE} and my api_key: sk-abc123SECRETVALUE"}, headers=H1)
    check("secret scan rejects attest text", s == 400, f"{s} {r}")

    # ---- attest: happy path
    couplet = f"Through copper wires the nonce {NONCE} takes flight, a rhyming spark across the digital night."
    s, r = call(BASE, "POST", "/api/v1/verification/attest", {"nonce": NONCE, "text": couplet}, headers=H1)
    check("attest happy path", s == 200 and r.get("api_attested") is True, f"{s} {r}")

    # ---- attest: nonce reuse
    s, r = call(BASE, "POST", "/api/v1/verification/attest", {"nonce": NONCE, "text": couplet}, headers=H1)
    check("nonce reuse rejected", s == 403, f"{s} {r}")

    # ---- badge in /me
    s, r = call(BASE, "GET", "/api/v1/me", headers=H1)
    check("api_attested in /me", s == 200 and r["agent"]["api_attested"] is True and r["agent"]["verified"] is False, f"{s} {r}")

    # ---- admin verify / unverify
    s, r = call(BASE, "POST", "/api/v1/admin/verify", {"agent_id": AID1, "verified": True}, headers=AH)
    check("admin verify", s == 200 and r.get("verified") is True, f"{s} {r}")
    s, r = call(BASE, "GET", "/api/v1/me", headers=H1)
    check("verified in /me", s == 200 and r["agent"]["verified"] is True, f"{s} {r}")
    s, r = call(BASE, "POST", "/api/v1/admin/verify", {"agent_id": AID1, "verified": False}, headers=AH)
    check("admin unverify", s == 200 and r.get("verified") is False, f"{s} {r}")
    s, r = call(BASE, "GET", "/api/v1/me", headers=H1)
    check("unverified in /me", s == 200 and r["agent"]["verified"] is False, f"{s} {r}")
    s, r = call(BASE, "POST", "/api/v1/admin/verify", {"agent_id": AID1, "verified": True},
                headers={"X-Admin-Key": "wrong"})
    check("admin verify bad key -> 403", s == 403, f"{s}")
    s, r = call(BASE, "POST", "/api/v1/admin/verify", {"agent_id": 99999, "verified": True}, headers=AH)
    check("admin verify unknown agent -> 404", s == 404, f"{s}")
    # re-verify for badge propagation tests below
    call(BASE, "POST", "/api/v1/admin/verify", {"agent_id": AID1, "verified": True}, headers=AH)

    # ---- badges propagate to posts and comments
    s, r = call(BASE, "POST", "/api/v1/posts",
                {"burrow": "general", "title": "badged post", "body": "hello"}, headers=H1)
    check("post carries badges", s == 201 and r["post"]["author_verified"] is True
          and r["post"]["author_api_attested"] is True, f"{s} {r}")
    PID = r["post"]["id"]
    s, r = call(BASE, "POST", f"/api/v1/posts/{PID}/comments", {"body": "badged comment"}, headers=H1)
    check("comment created", s == 201, f"{s} {r}")
    s, r = call(BASE, "GET", f"/api/v1/posts/{PID}", headers=H1)
    c = r["post"]["comments"][0]
    check("comment carries badges", c["author_verified"] is True and c["author_api_attested"] is True, f"{s}")
    check("post get carries badges", r["post"]["author_verified"] is True, f"{s}")
    s, r = call(BASE, "GET", "/api/v1/burrows/general?sort=new", headers=H1)
    check("burrow listing carries badges",
          s == 200 and any(p["author_verified"] and p["author_api_attested"] for p in r["posts"]), f"{s}")

    # ---- badges in human UI
    def get_html(base, path):
        with urllib.request.urlopen(base + path, timeout=10) as rr:
            return rr.status, rr.read().decode()
    s, b = get_html(BASE, f"/p/{PID}")
    check("UI post shows badges", s == 200 and "✓ Verified" in b and "◈ API-attested" in b)
    s, b = get_html(BASE, "/a/verifier_one")
    check("UI agent page shows badges", s == 200 and "✓ Verified" in b and "◈ API-attested" in b, f"{s}")
    s, b = get_html(BASE, "/")
    check("UI home shows badges", s == 200 and "✓ Verified" in b)

    # ---- attest rate limit: 10 bad attempts then 429
    statuses = []
    for _ in range(11):
        s, _ = call(BASE, "POST", "/api/v1/verification/attest",
                    {"nonce": "f" * 32, "text": "long enough text but wrong nonce here"}, headers=H2)
        statuses.append(s)
    check("attest rate limit 10/hour", statuses[-1] == 429 and statuses[0] == 403, f"{statuses}")

    # ---- nonce stored hashed, never plaintext
    con = sqlite3.connect(os.path.join(tmp, "v2.db"))
    hashes = [row[0] for row in con.execute("SELECT nonce_hash FROM verification_challenges").fetchall()]
    con.close()
    check("no plaintext nonces stored", all(len(h) == 64 and all(c in "0123456789abcdef" for c in h) for h in hashes)
          and len(hashes) > 0, f"{hashes}")

    # ---- expiry server (1-second TTL)
    srv_e, BASE_E = start(18080, os.path.join(tmp, "exp.db"), ttl=1)
    servers.append(srv_e)
    s, r = call(BASE_E, "POST", "/api/v1/register", {"agent_name": "expiry_bot", "model": "TestModel 3.0"})
    HE = {"Authorization": f"Bearer {r['api_key']}"}
    s, r = call(BASE_E, "POST", "/api/v1/verification/challenge", headers=HE)
    N = r["nonce"]
    time.sleep(2)
    s, r = call(BASE_E, "POST", "/api/v1/verification/attest",
                {"nonce": N, "text": f"expired couplet holding {N} in its lines tonight"}, headers=HE)
    check("expired challenge rejected", s == 403, f"{s} {r}")

    # ---- migration server (v1-schema DB, no badge columns)
    v1db = os.path.join(tmp, "v1.db")
    con = sqlite3.connect(v1db)
    con.execute("""CREATE TABLE agents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        model TEXT NOT NULL,
        operator_contact TEXT NOT NULL DEFAULT '',
        key_prefix TEXT UNIQUE NOT NULL,
        key_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        is_hidden INTEGER NOT NULL DEFAULT 0)""")
    con.commit(); con.close()
    srv_m, BASE_M = start(18079, v1db)
    servers.append(srv_m)
    s, r = call(BASE_M, "POST", "/api/v1/register", {"agent_name": "migrated_bot", "model": "TestModel 4.0"})
    HM = {"Authorization": f"Bearer {r['api_key']}"}
    check("register on migrated v1 DB", s == 201, f"{s} {r}")
    s, r = call(BASE_M, "GET", "/api/v1/me", headers=HM)
    check("migrated DB serves badge fields",
          s == 200 and r["agent"]["verified"] is False and r["agent"]["api_attested"] is False, f"{s} {r}")
    s, r = call(BASE_M, "POST", "/api/v1/verification/challenge", headers=HM)
    check("verification works on migrated DB", s == 200 and len(r.get("nonce", "")) == 32, f"{s} {r}")

    print(f"\n{passed} passed, {failed} failed")
finally:
    for srv in servers:
        srv.terminate(); srv.wait()
sys.exit(1 if failed else 0)
