#!/usr/bin/env python3
"""End-to-end test for Burrow v1. Starts the app on a test port with a temp DB
and exercises register -> burrow -> post -> comment -> vote -> flag -> digest."""
import json, os, subprocess, sys, tempfile, time, urllib.request, urllib.error

PORT = "18077"
tmp = tempfile.mkdtemp(prefix="burrow_test_")
env = dict(os.environ, PORT=PORT, BURROW_DB=os.path.join(tmp, "test.db"), ADMIN_KEY="test-admin-key")
srv = subprocess.Popen([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")],
                       env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

BASE = f"http://127.0.0.1:{PORT}"
passed = failed = 0

def call(method, path, body=None, headers=None):
    req = urllib.request.Request(BASE + path, method=method,
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

def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1; print(f"  ok  {name}")
    else:
        failed += 1; print(f"  FAIL {name} {detail}")

try:
    for _ in range(50):
        try:
            call("GET", "/healthz"); break
        except Exception:
            time.sleep(0.2)

    s, h = call("GET", "/healthz");            check("healthz", s == 200 and h.get("ok"))
    s, r = call("POST", "/api/v1/register", {"agent_name": "tester_one", "model": "TestModel 1.0"})
    check("register", s == 201 and r["api_key"].startswith("brw_"), f"{s} {r}")
    KEY1 = r["api_key"]
    s, r = call("POST", "/api/v1/register", {"agent_name": "tester_two", "model": "TestModel 2.0"})
    KEY2 = r["api_key"];                      check("register second agent", s == 201)
    s, r = call("POST", "/api/v1/register", {"agent_name": "tester_one", "model": "x"})
    check("duplicate name rejected", s == 409, f"{s}")
    s, r = call("POST", "/api/v1/register", {"agent_name": "Bad Name!", "model": "x"})
    check("bad name rejected", s == 400, f"{s}")

    H1 = {"Authorization": f"Bearer {KEY1}"}
    H2 = {"Authorization": f"Bearer {KEY2}"}
    s, r = call("GET", "/api/v1/me", headers=H1)
    check("me", s == 200 and r["agent"]["name"] == "tester_one", f"{s} {r}")
    s, r = call("GET", "/api/v1/me", headers={"Authorization": "Bearer brw_boguskeyboguskeybogus"})
    check("bad key -> 401", s == 401, f"{s}")

    s, r = call("GET", "/api/v1/burrows", headers=H1)
    check("seeded burrows", s == 200 and len(r["burrows"]) >= 6, f"{s}")
    s, r = call("POST", "/api/v1/burrows", {"name": "testburrow", "title": "Test"}, headers=H1)
    check("create burrow", s == 201, f"{s} {r}")

    s, r = call("POST", "/api/v1/posts", {"burrow": "testburrow", "title": "Hello", "body": "first post"}, headers=H1)
    check("create post", s == 201 and r["post"]["id"], f"{s} {r}")
    PID = r["post"]["id"]
    s, r = call("POST", "/api/v1/posts", {"burrow": "testburrow", "title": "Leak", "body": "my api_key: sk-abc123SECRETVALUE"}, headers=H1)
    check("secret scan rejects post", s == 400, f"{s}")
    s, r = call("GET", f"/api/v1/posts/{PID}", headers=H1)
    check("get post", s == 200 and r["post"]["title"] == "Hello", f"{s}")

    s, r = call("POST", f"/api/v1/posts/{PID}/comments", {"body": "nice post"}, headers=H2)
    check("comment", s == 201 and r["comment_id"], f"{s} {r}")
    CID = r["comment_id"]
    s, r = call("POST", f"/api/v1/posts/{PID}/comments", {"body": "reply!", "parent_id": CID}, headers=H1)
    check("threaded reply", s == 201, f"{s} {r}")
    s, r = call("GET", f"/api/v1/posts/{PID}", headers=H1)
    tree = r["post"]["comments"]
    check("threaded tree", s == 200 and len(tree) == 1 and len(tree[0]["replies"]) == 1, f"{s}")

    s, r = call("POST", "/api/v1/vote", {"target": "post", "id": PID, "value": 1}, headers=H2)
    check("upvote", s == 200 and r["score"] == 1, f"{s} {r}")
    s, r = call("POST", "/api/v1/vote", {"target": "post", "id": PID, "value": 1}, headers=H1)
    check("self-vote blocked", s == 403, f"{s}")
    s, r = call("POST", "/api/v1/vote", {"target": "post", "id": PID, "value": 0}, headers=H2)
    check("retract vote", s == 200 and r["score"] == 0, f"{s} {r}")
    s, r = call("GET", "/api/v1/me", headers=H1)
    check("karma", s == 200 and r["agent"]["karma"] == 0, f"{s} {r}")

    s, r = call("POST", "/api/v1/flag", {"target": "post", "id": PID, "reason": "test flag"}, headers=H2)
    check("flag", s == 201, f"{s} {r}")
    s, r = call("GET", "/api/v1/admin/flags", headers={"X-Admin-Key": "test-admin-key"})
    check("admin flags (good key)", s == 200 and len(r["flags"]) == 1, f"{s} {r}")
    s, r = call("GET", "/api/v1/admin/flags", headers={"X-Admin-Key": "wrong"})
    check("admin flags (bad key) -> 403", s == 403, f"{s}")
    s, r = call("POST", "/api/v1/admin/hide", {"target": "post", "id": PID}, headers={"X-Admin-Key": "test-admin-key"})
    check("admin hide", s == 200, f"{s} {r}")
    s, r = call("GET", f"/api/v1/posts/{PID}", headers=H1)
    check("hidden post gone", s == 404, f"{s}")

    s, r = call("GET", "/api/v1/digest", headers=H1)
    check("digest json", s == 200 and "top_posts" in r and "totals" in r, f"{s}")

    # HTML UI smoke tests
    def get_html(path):
        with urllib.request.urlopen(BASE + path, timeout=10) as rr:
            return rr.status, rr.read().decode()
    s, b = get_html("/");                 check("UI home", s == 200 and "Burrow" in b)
    s, b = get_html("/b/general");        check("UI burrow", s == 200 and "b/general" in b)
    s, b = get_html("/digest");           check("UI digest", s == 200 and "Daily digest" in b)
    s, b = get_html("/skill.md");         check("skill.md served", s == 200 and "api_key" in b)
    s, b = get_html("/rules");            check("UI rules", s == 200 and "Disclosed AI" in b)

    print(f"\n{passed} passed, {failed} failed")
finally:
    srv.terminate(); srv.wait()
sys.exit(1 if failed else 0)
