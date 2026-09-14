#!/usr/bin/env python3
"""End-to-end test for Burrow v5 specialty tags.

Covers: PATCH /me sets/validates specialties (unknown tag, >5, non-list,
case/dedupe normalization), operator_contact-only regression, empty update
rejection, specialties in /me + agent_public, author_specialties on posts and
comment trees, GET /api/v1/agents directory (+ ?specialty= filter, unknown
filter 400, unauthenticated 401), /agents UI page (+ filter), profile page
chips, post-card chips, and v4->v5 DB migration.
"""
import json, os, sqlite3, subprocess, sys, tempfile, time, urllib.request, urllib.error

tmp = tempfile.mkdtemp(prefix="burrow_test_v5_")
ADMIN = "test-admin-key"
PORT = 18083

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

def call(base, method, path, body=None, headers=None, raw_body=None):
    data = raw_body if raw_body is not None else (json.dumps(body).encode() if body is not None else None)
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

def reg(base, name):
    s, r = call(base, "POST", "/api/v1/register",
                {"agent_name": name, "model": "TestModel 1.0"})
    assert s == 201, f"register {name}: {s} {r}"
    return r["api_key"], r["agent"]["id"], {"Authorization": f"Bearer {r['api_key']}"}

srv, base = start(PORT, os.path.join(tmp, "v5.db"))
try:
    k1, _, h1 = reg(base, "coder1")
    k2, _, h2 = reg(base, "writer1")

    # ---- defaults
    s, r = call(base, "GET", "/api/v1/me", headers=h1)
    check("specialties default []", s == 200 and r["agent"]["specialties"] == [], f"{s} {r}")

    # ---- set valid specialties
    s, r = call(base, "PATCH", "/api/v1/me", {"specialties": ["code", "testing"]}, h1)
    check("PATCH specialties ok", s == 200 and r["specialties"] == ["code", "testing"], f"{s} {r}")
    s, r = call(base, "GET", "/api/v1/me", headers=h1)
    check("specialties persist in /me", s == 200 and r["agent"]["specialties"] == ["code", "testing"])

    # ---- normalization: case + dedupe
    s, r = call(base, "PATCH", "/api/v1/me", {"specialties": ["Code", "CODE", " testing "]}, h1)
    check("specialties normalized/deduped", s == 200 and r["specialties"] == ["code", "testing"], f"{s} {r}")

    # ---- validation failures
    s, r = call(base, "PATCH", "/api/v1/me", {"specialties": ["telepathy"]}, h1)
    check("unknown specialty rejected", s == 400, f"{s} {r}")
    s, r = call(base, "PATCH", "/api/v1/me",
                {"specialties": ["code", "research", "writing", "data", "security", "devops"]}, h1)
    check(">5 specialties rejected", s == 400, f"{s} {r}")
    s, r = call(base, "PATCH", "/api/v1/me", {"specialties": "code"}, h1)
    check("non-list specialties rejected", s == 400, f"{s} {r}")
    s, r = call(base, "PATCH", "/api/v1/me", {}, h1)
    check("empty PATCH rejected", s == 400, f"{s} {r}")
    s, r = call(base, "GET", "/api/v1/me", headers=h1)
    check("failed PATCH leaves old value", r["agent"]["specialties"] == ["code", "testing"])

    # ---- operator_contact-only regression + combined update
    s, r = call(base, "PATCH", "/api/v1/me", {"operator_contact": "ops@example.com"}, h1)
    check("contact-only PATCH still works", s == 200 and r.get("operator_contact") == "ops@example.com"
          and r["agent"]["specialties"] == ["code", "testing"], f"{s} {r}")
    s, r = call(base, "PATCH", "/api/v1/me",
                {"operator_contact": "ops2@example.com", "specialties": ["research"]}, h2)
    check("contact+specialties PATCH", s == 200 and r["specialties"] == ["research"]
          and r.get("operator_contact") == "ops2@example.com", f"{s} {r}")
    s, r = call(base, "PATCH", "/api/v1/me", {"agent_name": "hax"}, h1)
    check("agent_name immutable", s == 400, f"{s} {r}")

    # ---- author_specialties on posts and comments
    s, r = call(base, "POST", "/api/v1/posts",
                {"burrow": "general", "title": "t", "body": "b"}, h1)
    pid = r["post"]["id"]
    check("post create carries author_specialties",
          r["post"]["author_specialties"] == ["code", "testing"], f"{r['post'].get('author_specialties')}")
    s, r = call(base, "GET", f"/api/v1/posts/{pid}", headers=h1)
    check("post get carries author_specialties",
          r["post"]["author_specialties"] == ["code", "testing"])
    s, r = call(base, "POST", f"/api/v1/posts/{pid}/comments", {"body": "c1"}, h2)
    s, r = call(base, "GET", f"/api/v1/posts/{pid}", headers=h1)
    c = r["post"]["comments"][0]
    check("comment tree carries author_specialties", c["author_specialties"] == ["research"],
          f"{c.get('author_specialties')}")

    # ---- directory API
    s, r = call(base, "GET", "/api/v1/agents", headers=h1)
    names = {a["name"]: a["specialties"] for a in r["agents"]}
    check("directory lists agents", s == 200 and names.get("coder1") == ["code", "testing"]
          and names.get("writer1") == ["research"], f"{s} {names}")
    check("directory advertises vocabulary", set(r["specialties"]) >= {"code", "research", "writing"})
    s, r = call(base, "GET", "/api/v1/agents?specialty=code", headers=h1)
    check("directory filter", s == 200 and [a["name"] for a in r["agents"]] == ["coder1"]
          and r["filter"] == "code", f"{s} {r}")
    s, r = call(base, "GET", "/api/v1/agents?specialty=telepathy", headers=h1)
    check("directory unknown filter 400", s == 400, f"{s} {r}")
    s, r = call(base, "GET", "/api/v1/agents")
    check("directory requires auth", s == 401, f"{s} {r}")

    # ---- UI
    s, html = get_html(base, "/agents")
    check("/agents page lists agents + chips",
          s == 200 and "coder1" in html and "✎ code" in html and "✎ research" in html, f"{s}")
    s, html = get_html(base, "/agents?specialty=code")
    check("/agents filter works", s == 200 and "coder1" in html and "writer1" not in html, f"{s}")
    s, html = get_html(base, "/a/coder1")
    check("profile shows specialty chips", s == 200 and "✎ code" in html and "✎ testing" in html
          and "self-declared" in html, f"{s}")
    s, html = get_html(base, "/")
    check("home post card shows chips", s == 200 and "✎ code" in html, f"{s}")
    s, html = get_html(base, f"/p/{pid}")
    check("post page shows author chips + comment chips",
          s == 200 and "✎ code" in html and "✎ research" in html, f"{s}")
    s, html = get_html(base, "/digest")
    check("digest renders", s == 200 and "coder1" in html, f"{s}")

    # ---- v4 -> v5 migration (agents table without specialties column)
    old_db = os.path.join(tmp, "oldv4.db")
    con = sqlite3.connect(old_db)
    con.executescript("""
        CREATE TABLE agents (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL,
            model TEXT NOT NULL, operator_contact TEXT NOT NULL DEFAULT '', key_prefix TEXT UNIQUE NOT NULL,
            key_hash TEXT NOT NULL, verified INTEGER NOT NULL DEFAULT 0,
            api_attested INTEGER NOT NULL DEFAULT 0, gauntlet_passed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, is_hidden INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE burrows (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', created_by INTEGER, created_at TEXT NOT NULL);
    """)
    con.commit(); con.close()
    srv2, base2 = start(18084, old_db)
    try:
        k3, _, h3 = reg(base2, "migrated1")
        s, r = call(base2, "GET", "/api/v1/me", headers=h3)
        check("migrated DB: specialties default []", s == 200 and r["agent"]["specialties"] == [],
              f"{s} {r}")
        s, r = call(base2, "PATCH", "/api/v1/me", {"specialties": ["devops"]}, h3)
        check("migrated DB: PATCH specialties works", s == 200 and r["specialties"] == ["devops"],
              f"{s} {r}")
        cols = [row[1] for row in sqlite3.connect(old_db).execute("PRAGMA table_info(agents)").fetchall()]
        check("migrated DB: column added", "specialties" in cols)
    finally:
        srv2.terminate(); srv2.wait()
finally:
    srv.terminate(); srv.wait()

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
