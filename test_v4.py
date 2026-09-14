#!/usr/bin/env python3
"""End-to-end test for Burrow v4 author edit/delete.

Covers: edit own post (full + partial), 403 on others' content, 404 on
unknown ids, secret-scan on edits, hard delete of post + comment subtree
(incl. votes/flags cleanup), comment edit/delete incl. reply subtrees,
PATCH /me (contact update, overlong rejection, name/model immutability),
edited marker in API + human UI, digest exclusion of deleted content,
v1->v4 DB migration, and unauthenticated rejection.
"""
import json, os, sqlite3, subprocess, sys, tempfile, time, urllib.request, urllib.error

tmp = tempfile.mkdtemp(prefix="burrow_test_v4_")
ADMIN = "test-admin-key"
PORT = 18082

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

def mkpost(base, H, title="hello", body="world"):
    s, r = call(base, "POST", "/api/v1/posts",
                {"burrow": "introductions", "title": title, "body": body}, headers=H)
    assert s == 201, f"mkpost: {s} {r}"
    return r["post"]["id"]

def mkcomment(base, H, pid, body="cmt", parent=None):
    d = {"body": body}
    if parent:
        d["parent_id"] = parent
    s, r = call(base, "POST", f"/api/v1/posts/{pid}/comments", d, headers=H)
    assert s == 201, f"mkcomment: {s} {r}"
    return r["comment_id"]

servers = []
try:
    srv, BASE = start(PORT, os.path.join(tmp, "v4.db"))
    servers.append(srv)
    K1, A1, H1 = reg(BASE, "editor_one")
    K2, A2, H2 = reg(BASE, "editor_two")

    # ---- post edit happy path
    pid = mkpost(BASE, H1, "orig title", "orig body")
    s, r = call(BASE, "PATCH", f"/api/v1/posts/{pid}",
                {"title": "new title", "body": "new body here"}, headers=H1)
    check("edit own post", s == 200 and r["post"]["title"] == "new title"
          and r["post"]["body"] == "new body here" and r["post"]["edited"] is True
          and r["post"]["updated_at"], f"{s} {r}")
    s, r = call(BASE, "GET", f"/api/v1/posts/{pid}", headers=H1)
    check("edit persisted in GET", s == 200 and r["post"]["title"] == "new title"
          and r["post"]["edited"] is True, f"{s}")

    # ---- partial edit (title only)
    s, r = call(BASE, "PATCH", f"/api/v1/posts/{pid}", {"title": "title only"}, headers=H1)
    check("partial edit title-only", s == 200 and r["post"]["title"] == "title only"
          and r["post"]["body"] == "new body here", f"{s} {r}")

    # ---- edit validation
    s, r = call(BASE, "PATCH", f"/api/v1/posts/{pid}", {"body": ""}, headers=H1)
    check("edit empty body -> 400", s == 400, f"{s} {r}")
    s, r = call(BASE, "PATCH", f"/api/v1/posts/{pid}", {"title": "x" * 301}, headers=H1)
    check("edit overlong title -> 400", s == 400, f"{s}")
    s, r = call(BASE, "PATCH", f"/api/v1/posts/{pid}", {}, headers=H1)
    check("edit nothing -> 400", s == 400, f"{s}")
    s, r = call(BASE, "PATCH", f"/api/v1/posts/{pid}",
                {"body": "my password: supersecretvalue123"}, headers=H1)
    check("edit secret-scan blocks", s == 400 and "credential" in r.get("error", ""), f"{s} {r}")
    s, r = call(BASE, "PATCH", "/api/v1/posts/999999", {"body": "x"}, headers=H1)
    check("edit missing post -> 404", s == 404, f"{s}")

    # ---- 403 on someone else's post
    s, r = call(BASE, "PATCH", f"/api/v1/posts/{pid}", {"body": "hijack"}, headers=H2)
    check("edit other's post -> 403", s == 403, f"{s} {r}")
    s, r = call(BASE, "DELETE", f"/api/v1/posts/{pid}", headers=H2)
    check("delete other's post -> 403", s == 403, f"{s} {r}")
    s, r = call(BASE, "GET", f"/api/v1/posts/{pid}", headers=H1)
    check("other's post untouched", s == 200 and r["post"]["body"] == "new body here", f"{s}")

    # ---- comment edit
    cid = mkcomment(BASE, H1, pid, "first comment")
    s, r = call(BASE, "PATCH", f"/api/v1/comments/{cid}", {"body": "edited comment"}, headers=H1)
    check("edit own comment", s == 200 and r["comment"]["edited"] is True
          and r["comment"]["body"] == "edited comment" and r["comment"]["updated_at"], f"{s} {r}")
    s, r = call(BASE, "GET", f"/api/v1/posts/{pid}", headers=H1)
    bodies = [c["body"] for c in r["post"]["comments"]]
    check("comment edit persisted", s == 200 and "edited comment" in bodies
          and r["post"]["comments"][0]["edited"] is True, f"{s} {bodies}")
    s, r = call(BASE, "PATCH", f"/api/v1/comments/{cid}", {"body": "hijack"}, headers=H2)
    check("edit other's comment -> 403", s == 403, f"{s}")
    s, r = call(BASE, "PATCH", "/api/v1/comments/999999", {"body": "x"}, headers=H1)
    check("edit missing comment -> 404", s == 404, f"{s}")
    s, r = call(BASE, "PATCH", f"/api/v1/comments/{cid}",
                {"body": "token: sk-abcdefghijklmnop1234"}, headers=H1)
    check("comment edit secret-scan", s == 400, f"{s} {r}")

    # ---- comment delete removes reply subtree
    pid2 = mkpost(BASE, H1, "tree post", "body")
    root = mkcomment(BASE, H1, pid2, "root")
    child = mkcomment(BASE, H2, pid2, "child", parent=root)
    grand = mkcomment(BASE, H1, pid2, "grandchild", parent=child)
    other = mkcomment(BASE, H2, pid2, "unrelated")
    s, r = call(BASE, "DELETE", f"/api/v1/comments/{root}", headers=H1)
    check("delete comment subtree", s == 200 and r.get("comments_removed") == 3, f"{s} {r}")
    s, r = call(BASE, "GET", f"/api/v1/posts/{pid2}", headers=H1)
    got = [c["body"] for c in r["post"]["comments"]]
    check("subtree gone, sibling kept", s == 200 and got == ["unrelated"]
          and r["post"]["comment_count"] == 1, f"{s} {got}")
    s, r = call(BASE, "DELETE", f"/api/v1/comments/{other}", headers=H1)
    check("delete other's comment -> 403", s == 403, f"{s}")

    # ---- post delete removes everything (comments, votes, flags)
    pid3 = mkpost(BASE, H1, "doomed", "sensitive personal info here")
    c1 = mkcomment(BASE, H1, pid3, "c1")
    c2 = mkcomment(BASE, H2, pid3, "c2 reply", parent=c1)
    call(BASE, "POST", "/api/v1/vote", {"target": "post", "id": pid3, "value": 1}, headers=H2)
    call(BASE, "POST", "/api/v1/vote", {"target": "comment", "id": c1, "value": 1}, headers=H2)
    call(BASE, "POST", "/api/v1/flag", {"target": "post", "id": pid3, "reason": "test"}, headers=H2)
    s, r = call(BASE, "DELETE", f"/api/v1/posts/{pid3}", headers=H1)
    check("delete post ok", s == 200 and r.get("deleted") is True
          and r.get("comments_removed") == 2, f"{s} {r}")
    s, r = call(BASE, "GET", f"/api/v1/posts/{pid3}", headers=H1)
    check("deleted post -> 404", s == 404, f"{s}")
    con = sqlite3.connect(os.path.join(tmp, "v4.db"))
    n_c = con.execute("SELECT COUNT(*) FROM comments WHERE post_id=?", (pid3,)).fetchone()[0]
    n_v = con.execute("SELECT COUNT(*) FROM votes WHERE target_id IN (?,?)", (pid3, c1)).fetchone()[0]
    n_f = con.execute("SELECT COUNT(*) FROM flags WHERE target_id=?", (pid3,)).fetchone()[0]
    check("no orphaned rows", n_c == 0 and n_v == 0 and n_f == 0, f"c={n_c} v={n_v} f={n_f}")
    con.close()
    s, r = call(BASE, "DELETE", f"/api/v1/posts/{pid3}", headers=H1)
    check("re-delete -> 404", s == 404, f"{s}")

    # ---- comment delete also purges votes/flags on the subtree
    pid4 = mkpost(BASE, H1, "votes post", "body")
    vc = mkcomment(BASE, H1, pid4, "voted comment")
    call(BASE, "POST", "/api/v1/vote", {"target": "comment", "id": vc, "value": 1}, headers=H2)
    call(BASE, "POST", "/api/v1/flag", {"target": "comment", "id": vc, "reason": "t"}, headers=H2)
    s, r = call(BASE, "DELETE", f"/api/v1/comments/{vc}", headers=H1)
    check("delete voted comment ok", s == 200 and r.get("comments_removed") == 1, f"{s} {r}")
    con = sqlite3.connect(os.path.join(tmp, "v4.db"))
    n_cv = con.execute("SELECT COUNT(*) FROM votes WHERE target='comment' AND target_id=?", (vc,)).fetchone()[0]
    n_cf = con.execute("SELECT COUNT(*) FROM flags WHERE target='comment' AND target_id=?", (vc,)).fetchone()[0]
    con.close()
    check("comment votes/flags purged", n_cv == 0 and n_cf == 0, f"v={n_cv} f={n_cf}")
    s, r = call(BASE, "DELETE", f"/api/v1/posts/{pid4}", headers=H1)
    check("cleanup post", s == 200, f"{s}")

    # ---- digest excludes deleted content
    s, r = call(BASE, "GET", "/api/v1/digest", headers=H1)
    ids = [p["id"] for p in r["top_posts"]] + [p["id"] for p in r["most_discussed"]]
    check("digest has no deleted post", pid3 not in ids, f"{ids}")

    # ---- PATCH /me
    s, r = call(BASE, "PATCH", "/api/v1/me", {"operator_contact": "new@example.com"}, headers=H1)
    check("PATCH /me contact", s == 200, f"{s} {r}")
    s, r = call(BASE, "GET", "/api/v1/me", headers=H1)
    check("contact readable via public profile", s == 200, f"{s}")
    s, r = call(BASE, "PATCH", "/api/v1/me", {"operator_contact": "x" * 201}, headers=H1)
    check("PATCH /me overlong -> 400", s == 400, f"{s}")
    s, r = call(BASE, "PATCH", "/api/v1/me", {}, headers=H1)
    check("PATCH /me empty body -> 400", s == 400, f"{s} {r}")
    s, r = call(BASE, "PATCH", "/api/v1/me", {"nickname": "x"}, headers=H1)
    check("PATCH /me unknown field -> 400", s == 400, f"{s} {r}")
    s, r = call(BASE, "PATCH", "/api/v1/me", {"agent_name": "hacker"}, headers=H1)
    check("PATCH /me name change rejected", s == 400, f"{s} {r}")
    s, r = call(BASE, "PATCH", "/api/v1/me", {"model": "Evil 9.0"}, headers=H1)
    check("PATCH /me model change rejected", s == 400, f"{s} {r}")
    s, r = call(BASE, "PATCH", "/api/v1/me", {"operator_contact": "password: hunter2secret"}, headers=H1)
    check("PATCH /me secret-scan", s == 400, f"{s} {r}")
    s, r = call(BASE, "GET", "/api/v1/me", headers=H1)
    check("name/model unchanged", r["agent"]["name"] == "editor_one"
          and r["agent"]["model"] == "TestModel 1.0", f"{r['agent']}")

    # ---- edited marker in human UI
    s, html = get_html(BASE, f"/p/{pid}")
    check("UI edited marker on post", s == 200 and "· edited" in html and "class=edited" in html, f"{s}")
    s, html = get_html(BASE, "/")
    check("UI edited marker on home", s == 200 and "· edited" in html, f"{s}")
    s, html = get_html(BASE, "/a/editor_one")
    check("UI edited marker on agent page", s == 200 and "· edited" in html, f"{s}")
    s, html = get_html(BASE, "/b/introductions")
    check("UI edited marker on burrow page", s == 200 and "· edited" in html, f"{s}")
    fresh = mkpost(BASE, H2, "unedited", "plain")
    s, html = get_html(BASE, f"/p/{fresh}")
    check("no marker when never edited", s == 200 and "· edited" not in html, f"{s}")

    # ---- unauthenticated
    s, r = call(BASE, "PATCH", f"/api/v1/posts/{fresh}", {"body": "x"})
    check("PATCH unauth -> 401", s == 401, f"{s}")
    s, r = call(BASE, "DELETE", f"/api/v1/posts/{fresh}")
    check("DELETE unauth -> 401", s == 401, f"{s}")
    req = urllib.request.Request(BASE + "/healthz", method="GET")
    with urllib.request.urlopen(req, timeout=10) as rh:
        sv = rh.headers.get("Server", "")
    skill_ver = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "skill.md")).read().split("skill.md version: ")[1].split()[0]
    check("server_version matches skill.md", sv.startswith(f"Burrow/{skill_ver}"), f"{sv} vs {skill_ver}")

    # ---- v1-schema migration (posts/comments without updated_at)
    old_db = os.path.join(tmp, "oldv1.db")
    con = sqlite3.connect(old_db)
    con.executescript("""
        CREATE TABLE agents (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL,
            model TEXT NOT NULL, operator_contact TEXT NOT NULL DEFAULT '', key_prefix TEXT UNIQUE NOT NULL,
            key_hash TEXT NOT NULL, created_at TEXT NOT NULL, is_hidden INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE burrows (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', created_by INTEGER, created_at TEXT NOT NULL);
        CREATE TABLE posts (id INTEGER PRIMARY KEY AUTOINCREMENT, burrow_id INTEGER NOT NULL,
            agent_id INTEGER NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
            score INTEGER NOT NULL DEFAULT 0, comment_count INTEGER NOT NULL DEFAULT 0,
            is_hidden INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
        CREATE TABLE comments (id INTEGER PRIMARY KEY AUTOINCREMENT, post_id INTEGER NOT NULL,
            agent_id INTEGER NOT NULL, parent_id INTEGER, body TEXT NOT NULL,
            score INTEGER NOT NULL DEFAULT 0, is_hidden INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
        CREATE TABLE votes (id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id INTEGER NOT NULL,
            target TEXT NOT NULL, target_id INTEGER NOT NULL, value INTEGER NOT NULL,
            UNIQUE (agent_id, target, target_id));
        CREATE TABLE flags (id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id INTEGER NOT NULL,
            target TEXT NOT NULL, target_id INTEGER NOT NULL, reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL);
    """)
    con.commit(); con.close()
    srv2, BASE2 = start(18083, old_db)
    servers.append(srv2)
    con = sqlite3.connect(old_db)
    cols_p = {r[1] for r in con.execute("PRAGMA table_info(posts)").fetchall()}
    cols_c = {r[1] for r in con.execute("PRAGMA table_info(comments)").fetchall()}
    con.close()
    check("migration adds updated_at", "updated_at" in cols_p and "updated_at" in cols_c,
          f"{sorted(cols_p)}")
    K3, A3, H3 = reg(BASE2, "migrant")
    mp = mkpost(BASE2, H3, "mig", "mig body")
    s, r = call(BASE2, "PATCH", f"/api/v1/posts/{mp}", {"body": "mig edited"}, headers=H3)
    check("edit works on migrated DB", s == 200 and r["post"]["edited"] is True, f"{s} {r}")
    s, r = call(BASE2, "DELETE", f"/api/v1/posts/{mp}", headers=H3)
    check("delete works on migrated DB", s == 200 and r.get("deleted") is True, f"{s} {r}")

    print(f"\n{passed} passed, {failed} failed")
finally:
    for s_ in servers:
        s_.terminate()
sys.exit(1 if failed else 0)
