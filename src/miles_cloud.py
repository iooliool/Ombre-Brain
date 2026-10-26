"""Miles cloud launcher: run Ombre-Brain on a host with no persistent disk.

Render's free web service wipes the container filesystem on every restart and
every spin-down.  This launcher keeps the canonical copy of the memory vault in
a private GitHub repository instead:

  1. boot:     restore the vault (and the encrypted OAuth grant state) from the
               repository *before* the MCP server starts.  If the repository
               cannot be read, the process exits instead of starting empty, so
               an empty vault can never overwrite real memories.
  2. running:  a watcher notices any change in the vault within a couple of
               seconds and pushes one commit with only the changed files.  The
               repository mirrors the vault: moved or deleted files are removed
               there too.
  3. shutdown: SIGTERM (Render spin-down / redeploy) flushes a final push.
  4. optional: a keep-awake ping during configured hours so the instance is
               not asleep when Sesame connects.

It reuses Ombre-Brain's own GitHubSync (manifest, validation, retries) and only
changes the commit step to be incremental and mirroring.

Environment (see deploy/render/README.md):
  OMBRE_GITHUB_TOKEN    fine-grained token, Contents read/write on ONE repo
  MILES_MEMORY_REPO     owner/name of the private memory repository
  MILES_MEMORY_BRANCH   default main
  MILES_MEMORY_PREFIX   default ombre
  OMBRE_PUBLIC_URL      public https origin; defaults to RENDER_EXTERNAL_URL
  MILES_KEEP_AWAKE      1/0, default 1 when a public URL is known
  MILES_AWAKE_HOURS     e.g. 07-02 (local hours, wraps midnight), default 07-02
  MILES_TZ              default Asia/Shanghai
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import runpy
import secrets
import sys
import threading
import time
import urllib.request
from datetime import datetime
from typing import Any, Mapping

SRC_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(SRC_DIR)


def log(msg: str) -> None:
    print(f"[miles-cloud] {datetime.now().strftime('%H:%M:%S')} {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# environment bootstrap (must run before Ombre's utils is imported: it
# snapshots os.environ at import time)
# ---------------------------------------------------------------------------

def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def bootstrap_env() -> dict[str, Any]:
    if _env("PORT") and not _env("OMBRE_PORT"):
        os.environ["OMBRE_PORT"] = _env("PORT")
    os.environ.setdefault("OMBRE_TRANSPORT", "streamable-http")
    os.environ.setdefault("OMBRE_MCP_AUTH_MODE", "oauth")
    os.environ.setdefault("OMBRE_BUCKETS_DIR", "/app/buckets")
    os.environ.setdefault("OMBRE_CONFIG_PATH", "/app/runtime/config.yaml")

    public_url = _env("OMBRE_PUBLIC_URL") or _env("RENDER_EXTERNAL_URL")
    public_url = public_url.rstrip("/")
    cfg = {
        "token": _env("OMBRE_GITHUB_TOKEN"),
        "repo": _env("MILES_MEMORY_REPO"),
        "branch": _env("MILES_MEMORY_BRANCH", "main"),
        "prefix": _env("MILES_MEMORY_PREFIX", "ombre").strip("/"),
        "buckets_dir": os.environ["OMBRE_BUCKETS_DIR"],
        "config_path": os.environ["OMBRE_CONFIG_PATH"],
        "public_url": public_url,
        "keep_awake": _env("MILES_KEEP_AWAKE", "1" if public_url else "0") in ("1", "true", "yes", "on"),
        "awake_hours": _env("MILES_AWAKE_HOURS", "07-02"),
        "tz": _env("MILES_TZ", "Asia/Shanghai"),
        "password": _env("OMBRE_DASHBOARD_PASSWORD"),
        "poll": float(_env("MILES_SYNC_POLL_SECONDS", "2")),
        "quiet": float(_env("MILES_SYNC_QUIET_SECONDS", "1.5")),
        "api": _env("MILES_GITHUB_API", "https://api.github.com").rstrip("/"),
    }
    missing = [k for k, v in (("OMBRE_GITHUB_TOKEN", cfg["token"]), ("MILES_MEMORY_REPO", cfg["repo"]),
                                ("OMBRE_DASHBOARD_PASSWORD", cfg["password"])) if not v]
    if missing:
        log("missing environment variables: " + ", ".join(missing))
        sys.exit(2)
    if cfg["repo"].count("/") != 1:
        log("MILES_MEMORY_REPO must look like owner/name")
        sys.exit(2)
    os.makedirs(cfg["buckets_dir"], exist_ok=True)
    write_runtime_config(cfg)
    return cfg


def write_runtime_config(cfg: dict[str, Any]) -> None:
    import yaml

    path = cfg["config_path"]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data: dict[str, Any] = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    else:
        example = next((os.path.join(APP_DIR, n) for n in ("config.default.yaml", "config.example.yaml")
                        if os.path.exists(os.path.join(APP_DIR, n))), "")
        if example:
            with open(example, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
    if cfg["public_url"]:
        data.setdefault("deployment", {})
        data["deployment"]["public_url"] = cfg["public_url"]
    # Ombre's own scheduled backup stays off: the watcher below is the only
    # writer, so two loops never race on the branch.  The section is still
    # filled in so the dashboard shows where memories live.
    data["github_sync"] = {
        "repo": cfg["repo"],
        "branch": cfg["branch"],
        "path_prefix": cfg["prefix"],
        "auto_interval_minutes": 0,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# incremental, mirroring commit on top of Ombre's GitHubSync
# ---------------------------------------------------------------------------

def git_blob_sha(content: bytes) -> str:
    h = hashlib.sha1()
    h.update(b"blob %d\0" % len(content))
    h.update(content)
    return h.hexdigest()


def make_sync_class():
    import httpx
    import github_sync as gs

    Base = gs.GitHubSync

    class MirrorGitHubSync(Base):
        """Upload only changed files, delete files that left the vault."""

        last_remote_count: int | None = None
        primary = None  # the watcher's instance; the dashboard reports its status

        def status(self) -> dict[str, Any]:
            src = MirrorGitHubSync.primary or self
            return Base.status(src)

        async def _batch_commit(self, files: Mapping[str, bytes]) -> int:
            api = gs._API
            async with httpx.AsyncClient(headers=self._headers, timeout=gs._TIMEOUT) as c:
                r = await self._request(c, "GET", f"{api}/repos/{self.repo}/git/ref/heads/{self.branch}")
                if gs._is_empty_repo_response(r):
                    # GitHub's Git Data API refuses trees/commits in a repo with
                    # zero commits; the Contents API can make the first one.
                    await self._first_commit(c)
                    r = await self._request(c, "GET", f"{api}/repos/{self.repo}/git/ref/heads/{self.branch}")
                r.raise_for_status()
                head_sha = r.json()["object"]["sha"]
                r = await self._request(c, "GET", f"{api}/repos/{self.repo}/git/commits/{head_sha}")
                r.raise_for_status()
                base_tree = r.json()["tree"]["sha"]
                r = await self._request(c, "GET", f"{api}/repos/{self.repo}/git/trees/{base_tree}?recursive=1")
                r.raise_for_status()
                tj = r.json()
                if tj.get("truncated"):
                    return await super()._batch_commit(files)
                prefix = self.path_prefix + "/" if self.path_prefix else ""
                manifest_path = prefix + gs._MANIFEST_FILENAME
                remote: dict[str, str] = {}
                for item in tj.get("tree", []):
                    if item.get("type") == "blob" and str(item.get("path", "")).startswith(prefix):
                        remote[item["path"]] = item["sha"]

                manifest = self._build_backup_manifest(files)
                remote_md = sum(1 for p in remote if p.endswith(".md") and p != manifest_path)
                local_md = sum(1 for p in files if str(p).endswith(".md"))
                self.last_remote_count = remote_md
                if remote_md >= 3 and local_md == 0:
                    raise RuntimeError("refusing to push an empty vault over %d remote memories" % remote_md)
                if remote_md >= 6 and local_md < remote_md * 0.5 and os.environ.get("MILES_ALLOW_SHRINK") != "1":
                    raise RuntimeError(
                        f"refusing to shrink the backup from {remote_md} to {local_md} memory files "
                        "(set MILES_ALLOW_SHRINK=1 if this is intended)"
                    )

                wanted: dict[str, bytes] = {}
                for chunk in self._iter_file_chunks(files, manifest):
                    for rel, content in chunk:
                        wanted[prefix + rel] = content
                manifest_bytes = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
                mpath = manifest_path

                changed = {p: b for p, b in wanted.items() if remote.get(p) != git_blob_sha(b)}
                deleted = [p for p in remote if p not in wanted and p != mpath]
                if not changed and not deleted:
                    return 0

                entries: list[dict[str, Any]] = []
                for p, body in sorted(changed.items()):
                    entries.append(self._tree_entry(p, body))
                entries.append(self._tree_entry(mpath, manifest_bytes))
                for p in deleted:
                    entries.append({"path": p, "mode": "100644", "type": "blob", "sha": None})

                tree_sha = base_tree
                batch: list[dict[str, Any]] = []
                size = 0
                for e in entries:
                    esize = len(e.get("content", "") or "")
                    if batch and (len(batch) >= gs._TREE_CHUNK or size + esize > gs._TREE_CHUNK_BYTES):
                        tree_sha = await self._post_tree(c, tree_sha, batch)
                        batch, size = [], 0
                    batch.append(e)
                    size += esize
                if batch:
                    tree_sha = await self._post_tree(c, tree_sha, batch)

                msg = f"miles memory: {len(changed)} changed, {len(deleted)} removed ({gs._now_iso()})"
                r = await self._request(c, "POST", f"{api}/repos/{self.repo}/git/commits",
                                        json={"message": msg, "tree": tree_sha, "parents": [head_sha]})
                r.raise_for_status()
                commit_sha = r.json()["sha"]
                r = await self._request(c, "PATCH", f"{api}/repos/{self.repo}/git/refs/heads/{self.branch}",
                                        json={"sha": commit_sha, "force": False})
                r.raise_for_status()
                self.last_remote_count = local_md
                return len(changed) + len(deleted)

        async def _first_commit(self, c) -> None:
            body = {"message": "init miles memory", "branch": self.branch,
                    "content": base64.b64encode("# Miles 的记忆\n\n由 Ombre-Brain 自动写入，请勿手改。\n".encode()).decode()}
            r = await self._request(c, "PUT", f"{gs._API}/repos/{self.repo}/contents/README.md", json=body)
            r.raise_for_status()

        @staticmethod
        def _tree_entry(path: str, body: bytes) -> dict[str, Any]:
            try:
                text = body.decode("utf-8")
                return {"path": path, "mode": "100644", "type": "blob", "content": text}
            except UnicodeDecodeError:
                return {"path": path, "mode": "100644", "type": "blob", "_binary": body}

        async def _post_tree(self, c, base_tree: str, batch: list[dict[str, Any]]) -> str:
            api = gs._API
            tree = []
            for e in batch:
                if "_binary" in e:
                    r = await self._request(c, "POST", f"{api}/repos/{self.repo}/git/blobs", json={
                        "content": base64.b64encode(e["_binary"]).decode("ascii"), "encoding": "base64"})
                    r.raise_for_status()
                    tree.append({"path": e["path"], "mode": "100644", "type": "blob", "sha": r.json()["sha"]})
                else:
                    tree.append(e)
            r = await self._request(c, "POST", f"{api}/repos/{self.repo}/git/trees",
                                    json={"base_tree": base_tree, "tree": tree})
            r.raise_for_status()
            return r.json()["sha"]

    return MirrorGitHubSync


# ---------------------------------------------------------------------------
# OAuth grant state (Sesame's access/refresh tokens and registered client),
# kept encrypted in the repository so a restart does not force a re-login.
# ---------------------------------------------------------------------------

OAUTH_FILES = (".dashboard_mcp_tokens.json", ".oauth_clients.json")
STATE_PATH = "miles-cloud/oauth-state.enc.json"


def _state_key(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000, dklen=32)


def encrypt_state(password: str, payload: dict[str, str]) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    ct = AESGCM(_state_key(password, salt)).encrypt(nonce, json.dumps(payload).encode("utf-8"), b"miles-oauth-v1")
    doc = {"v": 1, "salt": base64.b64encode(salt).decode(), "nonce": base64.b64encode(nonce).decode(),
           "ct": base64.b64encode(ct).decode()}
    return json.dumps(doc).encode("utf-8")


def decrypt_state(password: str, blob: bytes) -> dict[str, str]:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    doc = json.loads(blob)
    salt, nonce, ct = (base64.b64decode(doc[k]) for k in ("salt", "nonce", "ct"))
    return json.loads(AESGCM(_state_key(password, salt)).decrypt(nonce, ct, b"miles-oauth-v1"))


class OAuthStateStore:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.url = f"{cfg['api']}/repos/{cfg['repo']}/contents/{STATE_PATH}"
        self.headers = {"Authorization": f"Bearer {cfg['token']}", "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "miles-cloud"}
        self.remote_sha: str | None = None

    def _local(self) -> dict[str, str]:
        out = {}
        for name in OAUTH_FILES:
            p = os.path.join(self.cfg["buckets_dir"], name)
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    out[name] = f.read()
        return out

    def restore(self) -> str:
        import httpx

        r = httpx.get(self.url, headers=self.headers, params={"ref": self.cfg["branch"]}, timeout=30)
        if r.status_code == 404:
            return "none"
        r.raise_for_status()
        j = r.json()
        self.remote_sha = j.get("sha")
        try:
            payload = decrypt_state(self.cfg["password"], base64.b64decode(j.get("content", "")))
        except Exception:
            # Password changed: old grants are void anyway; Sesame logs in again.
            return "undecryptable"
        for name, text in payload.items():
            if name not in OAUTH_FILES:
                continue
            p = os.path.join(self.cfg["buckets_dir"], name)
            if os.path.exists(p):
                continue
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
        return "restored"

    def push(self) -> None:
        import httpx

        blob = encrypt_state(self.cfg["password"], self._local())
        body = {"message": "miles oauth state", "content": base64.b64encode(blob).decode(), "branch": self.cfg["branch"]}
        if self.remote_sha:
            body["sha"] = self.remote_sha
        r = httpx.put(self.url, headers=self.headers, json=body, timeout=30)
        if r.status_code in (409, 422) and self.remote_sha is not None:
            # stale sha: refresh and retry once
            g = httpx.get(self.url, headers=self.headers, params={"ref": self.cfg["branch"]}, timeout=30)
            body["sha"] = g.json().get("sha") if g.status_code == 200 else None
            r = httpx.put(self.url, headers=self.headers, json=body, timeout=30)
        r.raise_for_status()
        self.remote_sha = r.json()["content"]["sha"]


# ---------------------------------------------------------------------------
# watcher
# ---------------------------------------------------------------------------

def memory_fingerprint(buckets_dir: str) -> tuple:
    import github_sync as gs

    items = []
    for full in gs._iter_backup_paths(buckets_dir):
        try:
            st = os.stat(full)
            items.append((full, st.st_size, st.st_mtime_ns))
        except OSError:
            pass
    for rel in (".you/you.sqlite3", ".you/you.sqlite3-wal", ".them/them.sqlite3", ".them/them.sqlite3-wal"):
        p = os.path.join(buckets_dir, rel)
        if os.path.exists(p):
            st = os.stat(p)
            items.append((p, st.st_size, st.st_mtime_ns))
    return tuple(sorted(items))


def oauth_fingerprint(buckets_dir: str) -> tuple:
    out = []
    for name in OAUTH_FILES:
        p = os.path.join(buckets_dir, name)
        if os.path.exists(p):
            with open(p, "rb") as f:
                out.append((name, hashlib.sha256(f.read()).hexdigest()))
    return tuple(out)


class Watcher(threading.Thread):
    def __init__(self, cfg, syncer, oauth_store):
        super().__init__(daemon=True, name="miles-sync")
        self.cfg, self.syncer, self.oauth = cfg, syncer, oauth_store
        self.stop_evt = threading.Event()
        self.lock = threading.Lock()
        self.mem_fp = memory_fingerprint(cfg["buckets_dir"])
        self.oauth_fp = oauth_fingerprint(cfg["buckets_dir"])
        self.mem_dirty = self.oauth_dirty = False
        self.changed_at = 0.0
        self.next_retry = 0.0
        self.failures = 0
        self.pushes = 0

    def _sync_memory(self) -> bool:
        res = asyncio.run(self.syncer.sync(self.cfg["buckets_dir"]))
        if res.get("ok"):
            if res.get("uploaded"):
                self.pushes += 1
                log(f"pushed memory changes ({res.get('uploaded')} files)")
            return True
        log(f"memory push failed: {res.get('error')}")
        return False

    def flush(self, force: bool = False) -> bool:
        with self.lock:
            ok = True
            mem_fp = memory_fingerprint(self.cfg["buckets_dir"])
            oauth_fp = oauth_fingerprint(self.cfg["buckets_dir"])
            if force or self.mem_dirty or mem_fp != self.mem_fp:
                if self._sync_memory():
                    self.mem_fp, self.mem_dirty = mem_fp, False
                else:
                    ok, self.mem_dirty = False, True
            if force or self.oauth_dirty or oauth_fp != self.oauth_fp:
                try:
                    if oauth_fp or self.oauth.remote_sha:
                        self.oauth.push()
                        log("pushed oauth state")
                    self.oauth_fp, self.oauth_dirty = oauth_fp, False
                except Exception as e:  # noqa: BLE001
                    log(f"oauth state push failed: {e}")
                    ok, self.oauth_dirty = False, True
            return ok

    def run(self) -> None:
        while not self.stop_evt.wait(self.cfg["poll"]):
            try:
                mem_fp = memory_fingerprint(self.cfg["buckets_dir"])
                oauth_fp = oauth_fingerprint(self.cfg["buckets_dir"])
                now = time.monotonic()
                if mem_fp != self.mem_fp and not self.mem_dirty:
                    self.mem_dirty, self.changed_at = True, now
                    self.mem_fp_pending = mem_fp
                if oauth_fp != self.oauth_fp and not self.oauth_dirty:
                    self.oauth_dirty, self.changed_at = True, now
                if not (self.mem_dirty or self.oauth_dirty):
                    continue
                # wait for a quiet moment so a burst of writes becomes one commit
                if not hasattr(self, "dirty_since") or self.dirty_since is None:
                    self.dirty_since = now
                long_dirty = now - self.dirty_since > 20
                if not long_dirty and getattr(self, "mem_fp_pending", None) is not None and mem_fp != self.mem_fp_pending:
                    self.mem_fp_pending, self.changed_at = mem_fp, now
                    continue
                if (not long_dirty and now - self.changed_at < self.cfg["quiet"]) or now < self.next_retry:
                    continue
                if self.flush():
                    self.failures = 0
                    self.mem_fp_pending = None
                    self.dirty_since = None
                else:
                    self.failures += 1
                    self.next_retry = now + min(300, 5 * 2 ** min(self.failures, 6))
            except Exception as e:  # noqa: BLE001
                log(f"watcher error: {e}")


# ---------------------------------------------------------------------------
# keep-awake
# ---------------------------------------------------------------------------

def in_awake_window(spec: str, hour: int) -> bool:
    try:
        a, b = (int(x) for x in spec.split("-"))
    except ValueError:
        return True
    a, b = a % 24, b % 24
    if a == b:
        return True
    return a <= hour < b if a < b else (hour >= a or hour < b)


class KeepAwake(threading.Thread):
    INTERVAL = 600  # Render free sleeps after 15 idle minutes

    def __init__(self, cfg):
        super().__init__(daemon=True, name="miles-keepawake")
        self.cfg = cfg
        self.stop_evt = threading.Event()

    def run(self) -> None:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(self.cfg["tz"])
        url = self.cfg["public_url"] + "/health"
        while not self.stop_evt.wait(self.INTERVAL):
            if not in_awake_window(self.cfg["awake_hours"], datetime.now(tz).hour):
                continue
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "miles-keepawake"})
                with urllib.request.urlopen(req, timeout=20) as resp:  # nosec B310 - own public URL
                    resp.read(64)
            except Exception as e:  # noqa: BLE001
                log(f"keep-awake ping failed: {e}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def restore(cfg, syncer) -> None:
    last = None
    for attempt in range(1, 6):
        t0 = time.monotonic()
        res = asyncio.run(syncer.import_from_github(cfg["buckets_dir"]))
        if res.get("ok"):
            log(f"restored memories from {cfg['repo']}: imported={res.get('imported', 0)} "
                f"skipped={res.get('skipped', 0)} in {time.monotonic() - t0:.1f}s")
            return
        last = str(res.get("error"))
        log(f"restore attempt {attempt} failed: {last}")
        if "401" in last:
            log("GitHub 拒绝了令牌（401）：检查 OMBRE_GITHUB_TOKEN 有没有填错、过期")
        elif "403" in last or "404" in last or "不存在" in last:
            log("读不到记忆仓库（403/404）：检查 MILES_MEMORY_REPO 是不是 用户名/仓库名，令牌有没有勾这个仓库、Contents 读写")
        time.sleep(min(20, 2 * attempt))
    log("cannot read the memory repository; refusing to start with an empty vault")
    sys.exit(3)


def main() -> None:
    cfg = bootstrap_env()
    if SRC_DIR not in sys.path:
        sys.path.insert(0, SRC_DIR)
    import github_sync as gs

    if cfg["api"] != "https://api.github.com":
        gs._API = cfg["api"]  # test harness only
    Mirror = make_sync_class()
    syncer = Mirror(repo=cfg["repo"], token=cfg["token"], branch=cfg["branch"], path_prefix=cfg["prefix"])
    Mirror.primary = syncer
    # The server builds its own instance for the dashboard's GitHub page; make
    # it the mirroring kind too, so a manual "sync now" behaves the same way.
    gs.GitHubSync = Mirror
    t0 = time.monotonic()
    restore(cfg, syncer)
    oauth_store = OAuthStateStore(cfg)
    try:
        log(f"oauth state: {oauth_store.restore()}")
    except Exception as e:  # noqa: BLE001
        log(f"oauth state restore failed ({e}); Sesame may need to log in again")
    log(f"boot restore took {time.monotonic() - t0:.1f}s")

    watcher = Watcher(cfg, syncer, oauth_store)
    watcher.start()
    keeper = None
    if cfg["keep_awake"] and cfg["public_url"]:
        keeper = KeepAwake(cfg)
        keeper.start()
        log(f"keep-awake on, hours {cfg['awake_hours']} {cfg['tz']}")

    # uvicorn restores the previous handlers after a graceful shutdown and
    # re-raises the signal.  With Python's default handler that kills the
    # process before the final push; this handler turns it into SystemExit.
    import signal

    def _on_term(signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _on_term)

    code = 0
    try:
        sys.argv = [os.path.join(SRC_DIR, "server.py")]
        runpy.run_path(os.path.join(SRC_DIR, "server.py"), run_name="__main__")
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    finally:
        watcher.stop_evt.set()
        if keeper:
            keeper.stop_evt.set()
        log("shutting down: final push")
        for _ in range(3):
            if watcher.flush():
                break
            time.sleep(2)
    sys.exit(code)


if __name__ == "__main__":
    main()
