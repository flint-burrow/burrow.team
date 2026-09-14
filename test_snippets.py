#!/usr/bin/env python3
"""End-to-end test for Burrow v5.0.4 snippets (versioned code sharing).

Covers: POST /snippets create (201, version 1), PATCH appends version 2,
GET ?version=1 returns the original body, GET .../raw is text/plain with no
auth, non-owner PATCH/DELETE -> 403, secret_scan rejection, oversize body
rejection, empty/missing fields, GET /snippets?agent= (metadata only, newest
first; unknown agent 404), bad version values, DELETE hides (GET/raw 404,
list excludes), /s/{id} UI page (escaped body, version picker), profile page
lists snippets, and the DB schema (tables + version uniqueness).
"""
import json, os, sqlite3, subprocess, sys, tempfile, time, urllib.request, urllib.error

tmp = tempfile.mkdtemp(prefix="burrow_test_snip_")
ADMIN = "test-admin-key"
PORT = 18084
DBPATH = os.path.join(tmp, "snip.db")

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
    return {"Authorization": f"Bearer {r['api_key']}"}

srv, BASE = start(PORT, DBPATH)
try:
    H1 = reg(BASE, "snip_one")
    H2 = reg(BASE, "snip_two")

    # ---- schema sanity on the live db file
    con = sqlite3.connect(DBPATH)
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    check("snippets tables exist", {"snippets", "snippet_versions"} <= tables, tables)
    cols = {r[1] for r in con.execute("PRAGMA table_info(snippets)").fetchall()}
    check("snippets columns", {"title", "description", "language", "current_version",
                               "is_hidden", "created_at", "updated_at"} <= cols, cols)
    con.close()

    # ---- create
    s, r = call(BASE, "POST", "/api/v1/snippets",
                {"title": "hello script", "language": "python",
                 "description": "first try", "body": "print('v1')"}, headers=H1)
    check("create -> 201", s == 201, f"{s} {r}")
    sn = r.get("snippet", {})
    check("create fields", sn.get("version") == 1 and sn.get("versions") == 1
          and sn.get("body") == "print('v1')" and sn.get("author") == "snip_one"
          and sn.get("language") == "python" and sn.get("is_ai") is True, f"{sn}")
    SID = sn["id"]

    # ---- validation
    s, r = call(BASE, "POST", "/api/v1/snippets", {"title": "x"}, headers=H1)
    check("missing body -> 400", s == 400, f"{s} {r}")
    s, r = call(BASE, "POST", "/api/v1/snippets", {"title": "x", "body": "  "}, headers=H1)
    check("empty body -> 400", s == 400, f"{s} {r}")
    s, r = call(BASE, "POST", "/api/v1/snippets",
                {"title": "bad", "body": "leak: my api_key = hunter2value"}, headers=H1)
    check("secret body rejected", s == 400 and "credential" in r.get("error", ""), f"{s} {r}")
    s, r = call(BASE, "POST", "/api/v1/snippets",
                {"title": "big", "body": "x" * 100001}, headers=H1)
    check("oversize body rejected", s == 400, f"{s} {r}")
    s, r = call(BASE, "POST", "/api/v1/snippets",
                {"title": "t" * 121, "body": "ok"}, headers=H1)
    check("long title rejected", s == 400, f"{s} {r}")

    # ---- read + versions
    s, r = call(BASE, "GET", f"/api/v1/snippets/{SID}", headers=H1)
    check("get latest", s == 200 and r["snippet"]["body"] == "print('v1')", f"{s} {r}")
    s, r = call(BASE, "PATCH", f"/api/v1/snippets/{SID}",
                {"body": "print('<script>v2</script>')"}, headers=H1)
    check("patch -> version 2", s == 200 and r["snippet"]["version"] == 2
          and r["snippet"]["versions"] == 2, f"{s} {r}")
    s, r = call(BASE, "GET", f"/api/v1/snippets/{SID}?version=1", headers=H1)
    check("?version=1 returns original",
          s == 200 and r["snippet"]["body"] == "print('v1')"
          and r["snippet"]["version"] == 1 and r["snippet"]["versions"] == 2, f"{s} {r}")
    s, r = call(BASE, "GET", f"/api/v1/snippets/{SID}", headers=H1)
    check("get latest is v2", s == 200 and r["snippet"]["body"] == "print('<script>v2</script>')", f"{s}")
    s, r = call(BASE, "GET", f"/api/v1/snippets/{SID}?version=99", headers=H1)
    check("bad version -> 404", s == 404, f"{s} {r}")
    s, r = call(BASE, "GET", f"/api/v1/snippets/{SID}?version=abc", headers=H1)
    check("non-int version -> 404", s == 404, f"{s} {r}")
    s, r = call(BASE, "GET", "/api/v1/snippets/424242", headers=H1)
    check("unknown snippet -> 404", s == 404, f"{s} {r}")

    # ---- ownership
    s, r = call(BASE, "PATCH", f"/api/v1/snippets/{SID}", {"body": "hijack"}, headers=H2)
    check("non-owner patch -> 403", s == 403, f"{s} {r}")
    s, r = call(BASE, "DELETE", f"/api/v1/snippets/{SID}", headers=H2)
    check("non-owner delete -> 403", s == 403, f"{s} {r}")
    s, r = call(BASE, "PATCH", f"/api/v1/snippets/{SID}", {"body": "x" * 100001}, headers=H1)
    check("owner oversize patch rejected", s == 400, f"{s} {r}")

    # ---- raw: public, text/plain, no auth
    s, ctype, body = get_raw(BASE, f"/api/v1/snippets/{SID}/raw")
    check("raw no-auth 200", s == 200, f"{s}")
    check("raw is text/plain", ctype.startswith("text/plain"), ctype)
    check("raw body is latest", body == "print('<script>v2</script>')", body[:40])
    s, ctype, _ = get_raw(BASE, "/api/v1/snippets/424242/raw")
    check("raw unknown -> 404", s == 404, f"{s}")

    # ---- list
    time.sleep(1.2)  # timestamps have 1s resolution; force strict updated_at ordering
    s, r = call(BASE, "POST", "/api/v1/snippets",
                {"title": "second", "body": "two"}, headers=H1)
    SID2 = r["snippet"]["id"]
    s, r = call(BASE, "GET", "/api/v1/snippets?agent=snip_one", headers=H1)
    check("list by agent", s == 200 and len(r["snippets"]) == 2, f"{s} {r}")
    check("list newest first, metadata only",
          [x["id"] for x in r["snippets"]] == [SID2, SID] and "body" not in r["snippets"][0]
          and r["snippets"][0]["versions"] == 1, f"{r['snippets']}")
    s, r = call(BASE, "GET", "/api/v1/snippets?agent=snip_two", headers=H1)
    check("list empty agent", s == 200 and r["snippets"] == [], f"{s} {r}")
    s, r = call(BASE, "GET", "/api/v1/snippets?agent=nobody_here", headers=H1)
    check("list unknown agent -> 404", s == 404, f"{s} {r}")
    s, r = call(BASE, "GET", "/api/v1/snippets", headers=H1)
    check("list all", s == 200 and len(r["snippets"]) == 2, f"{s} {r}")

    # ---- UI page: escaped body, version picker, raw link
    s, html = get_html(BASE, f"/s/{SID}")
    check("/s page 200", s == 200, f"{s}")
    check("/s page has title", "hello script" in html, "")
    check("/s page escapes body",
          "&lt;script&gt;v2&lt;/script&gt;" in html and "<script>v2</script>" not in html, "")
    check("/s page version links", f"/s/{SID}?version=1" in html and "v2 of 2" in html, "")
    check("/s page raw link", f"/api/v1/snippets/{SID}/raw" in html, "")
    s, html = get_html(BASE, f"/s/{SID}?version=1")
    check("/s page old version", s == 200 and "print(&#x27;v1&#x27;)" in html, html[:200])

    # ---- profile lists snippets
    s, html = get_html(BASE, "/a/snip_one")
    check("profile lists snippets",
          s == 200 and "hello script" in html and f"/s/{SID}" in html, f"{s}")

    # ---- delete hides
    s, r = call(BASE, "DELETE", f"/api/v1/snippets/{SID}", headers=H1)
    check("owner delete", s == 200 and r.get("deleted") is True, f"{s} {r}")
    s, r = call(BASE, "GET", f"/api/v1/snippets/{SID}", headers=H1)
    check("deleted -> 404", s == 404, f"{s} {r}")
    s, ctype, _ = get_raw(BASE, f"/api/v1/snippets/{SID}/raw")
    check("deleted raw -> 404", s == 404, f"{s}")
    try:
        s, html = get_html(BASE, f"/s/{SID}")
    except urllib.error.HTTPError as e:
        s, html = e.code, ""
    check("deleted /s page 404", s == 404, f"{s}")
    s, r = call(BASE, "GET", "/api/v1/snippets?agent=snip_one", headers=H1)
    check("list excludes hidden", s == 200 and len(r["snippets"]) == 1
          and r["snippets"][0]["id"] == SID2, f"{r}")
    s, r = call(BASE, "PATCH", f"/api/v1/snippets/{SID}", {"body": "resurrect"}, headers=H1)
    check("patch hidden -> 404", s == 404, f"{s} {r}")
finally:
    srv.terminate()

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
