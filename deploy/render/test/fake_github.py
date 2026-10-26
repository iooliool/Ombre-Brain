"""A tiny in-memory stand-in for the GitHub REST endpoints Ombre's backup uses.

Only for local testing of src/miles_cloud.py: git refs/commits/trees/blobs and
the contents API for one file.  Usage: python fake_github.py PORT owner/repo [--empty]
GET /_debug/files lists the current branch tree.  Requests need a Bearer token
equal to $FAKE_GH_TOKEN (default test-token).
"""
import base64
import hashlib
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

PORT = int(sys.argv[1])
REPO = sys.argv[2]
EMPTY = "--empty" in sys.argv
TOKEN = os.environ.get("FAKE_GH_TOKEN", "test-token")

lock = threading.Lock()
blobs: dict[str, bytes] = {}
trees: dict[str, dict[str, str]] = {}      # tree sha -> {path: blob sha}
commits: dict[str, dict] = {}
refs: dict[str, str] = {}
stats = {"requests": 0, "commits": 0}


def blob_sha(b: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(b) + b).hexdigest()


def put_blob(b: bytes) -> str:
    s = blob_sha(b)
    blobs[s] = b
    return s


def put_tree(m: dict[str, str]) -> str:
    s = hashlib.sha1(json.dumps(sorted(m.items())).encode()).hexdigest()
    trees[s] = dict(m)
    return s


def put_commit(tree: str, parents: list[str], msg: str) -> str:
    s = hashlib.sha1(json.dumps([tree, parents, msg, len(commits)]).encode()).hexdigest()
    commits[s] = {"tree": tree, "parents": parents, "message": msg}
    stats["commits"] += 1
    return s


if not EMPTY:
    refs["main"] = put_commit(put_tree({"README.md": put_blob(b"# miles-memory\n")}), [], "Initial commit")


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _auth(self):
        if self.headers.get("Authorization", "").split(" ")[-1] != TOKEN:
            self._send(401, {"message": "Bad credentials"})
            return False
        return True

    def handle_any(self, method):
        stats["requests"] += 1
        u = urlparse(self.path)
        if u.path == "/_debug/files":
            with lock:
                head = refs.get("main")
                files = trees[commits[head]["tree"]] if head else {}
                out = {p: blobs[s].decode("utf-8", "replace") for p, s in files.items()}
            return self._send(200, {"files": out, "stats": stats})
        if not self._auth():
            return
        base = f"/repos/{REPO}"
        if not u.path.startswith(base):
            return self._send(404, {"message": "Not Found"})
        p = u.path[len(base):]
        with lock:
            m = re.fullmatch(r"/git/refs?/heads/(.+)", p)
            if m and method == "GET":
                if not refs:
                    return self._send(409, {"message": "Git Repository is empty."})
                if m.group(1) not in refs:
                    return self._send(404, {"message": "Not Found"})
                return self._send(200, {"object": {"sha": refs[m.group(1)]}})
            if m and method == "PATCH":
                b = self._body()
                cur = refs.get(m.group(1))
                if not b.get("force") and cur not in commits.get(b["sha"], {}).get("parents", []):
                    return self._send(422, {"message": "Update is not a fast forward"})
                refs[m.group(1)] = b["sha"]
                return self._send(200, {"object": {"sha": b["sha"]}})
            if p == "/git/refs" and method == "POST":
                b = self._body()
                refs[b["ref"].split("/")[-1]] = b["sha"]
                return self._send(201, {"object": {"sha": b["sha"]}})
            m = re.fullmatch(r"/git/commits/(\w+)", p)
            if m:
                c = commits.get(m.group(1))
                return self._send(200, {"sha": m.group(1), "tree": {"sha": c["tree"]}}) if c else self._send(404, {})
            if p == "/git/commits" and method == "POST":
                b = self._body()
                return self._send(201, {"sha": put_commit(b["tree"], b.get("parents", []), b["message"])})
            m = re.fullmatch(r"/git/trees/(\w+)", p)
            if m:
                t = trees.get(m.group(1))
                if t is None:
                    return self._send(404, {})
                return self._send(200, {"sha": m.group(1), "truncated": False, "tree": [
                    {"path": k, "mode": "100644", "type": "blob", "sha": v, "size": len(blobs[v])} for k, v in sorted(t.items())]})
            if p == "/git/trees" and method == "POST":
                b = self._body()
                if not refs:
                    return self._send(409, {"message": "Git Repository is empty."})
                t = dict(trees.get(b.get("base_tree") or "", {}))
                for e in b["tree"]:
                    if "content" in e:
                        t[e["path"]] = put_blob(e["content"].encode("utf-8"))
                    elif e.get("sha") is None:
                        t.pop(e["path"], None)
                    else:
                        t[e["path"]] = e["sha"]
                return self._send(201, {"sha": put_tree(t)})
            if p == "/git/blobs" and method == "POST":
                b = self._body()
                return self._send(201, {"sha": put_blob(base64.b64decode(b["content"]))})
            m = re.fullmatch(r"/git/blobs/(\w+)", p)
            if m:
                d = blobs.get(m.group(1))
                return self._send(200, {"sha": m.group(1), "size": len(d), "encoding": "base64",
                                        "content": base64.b64encode(d).decode()}) if d is not None else self._send(404, {})
            m = re.fullmatch(r"/contents/(.+)", p)
            if m:
                path = m.group(1)
                head = refs.get("main")
                files = dict(trees[commits[head]["tree"]]) if head else {}
                if method == "GET":
                    s = files.get(path)
                    if not s:
                        return self._send(404, {"message": "Not Found"})
                    return self._send(200, {"sha": s, "content": base64.b64encode(blobs[s]).decode()})
                if method == "PUT":
                    b = self._body()
                    if path in files and b.get("sha") != files[path]:
                        return self._send(409, {"message": "sha mismatch"})
                    files[path] = put_blob(base64.b64decode(b["content"]))
                    refs["main"] = put_commit(put_tree(files), [head] if head else [], b["message"])
                    return self._send(200, {"content": {"sha": files[path]}})
        return self._send(404, {"message": "Not Found"})

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")

    def do_PATCH(self):
        self.handle_any("PATCH")

    def do_PUT(self):
        self.handle_any("PUT")


ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
