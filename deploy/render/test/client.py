"""Test client that behaves like Sesame's "Add custom app": OAuth 2.1 + MCP.

  client.py login BASE PASSWORD TOKENFILE     full OAuth flow (wrong password must fail), saves tokens
  client.py refresh BASE TOKENFILE            refresh_token grant
  client.py call BASE TOKENFILE TOOL JSON     call one MCP tool, print the text
"""
import asyncio
import base64
import hashlib
import json
import secrets
import sys
import time
import urllib.parse

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

REDIRECT = "https://sesame.example/oauth/callback"


def login(base, pw, tokfile):
    with httpx.Client(timeout=30, follow_redirects=False) as c:
        r = c.post(f"{base}/mcp", json={})
        assert r.status_code == 401, r.status_code
        print("no token -> 401, WWW-Authenticate:", r.headers.get("www-authenticate", "")[:90])
        prm = c.get(f"{base}/.well-known/oauth-protected-resource/mcp")
        print("protected-resource metadata ->", prm.status_code)
        meta = c.get(f"{base}/.well-known/oauth-authorization-server").json()
        reg = c.post(meta["registration_endpoint"], json={
            "client_name": "sesame-like-test", "redirect_uris": [REDIRECT],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "token_endpoint_auth_method": "none"})
        cid = reg.json()["client_id"]
        print("dynamic client registration ->", reg.status_code)
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        q = dict(response_type="code", client_id=cid, redirect_uri=REDIRECT, state=secrets.token_hex(8),
                 code_challenge=challenge, code_challenge_method="S256", scope="mcp", resource=f"{base}/mcp")
        page = c.get(meta["authorization_endpoint"], params=q)
        print("authorize page ->", page.status_code, "asks password" if "password" in page.text else "")
        bad = c.post(meta["authorization_endpoint"], data={**q, "password": "wrong-password"})
        assert bad.status_code != 302, "wrong password was accepted"
        print("wrong password ->", bad.status_code, "(rejected)")
        ok = c.post(meta["authorization_endpoint"], data={**q, "password": pw})
        loc = ok.headers.get("location", "")
        code = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query).get("code", [""])[0]
        assert code, loc
        print("right password ->", ok.status_code, "got code")
        tok = c.post(meta["token_endpoint"], data=dict(
            grant_type="authorization_code", code=code, redirect_uri=REDIRECT,
            client_id=cid, code_verifier=verifier, resource=f"{base}/mcp")).json()
        assert tok.get("access_token"), tok
        tok["client_id"] = cid
        tok["token_endpoint"] = meta["token_endpoint"]
        json.dump(tok, open(tokfile, "w"))
        print("token exchange -> ok, expires_in", tok.get("expires_in"))


def refresh(base, tokfile):
    tok = json.load(open(tokfile))
    r = httpx.post(tok["token_endpoint"].replace(tok["token_endpoint"].split("/")[2], base.split("/")[2]),
                   data=dict(grant_type="refresh_token", refresh_token=tok["refresh_token"],
                             client_id=tok["client_id"], resource=f"{base}/mcp"), timeout=30)
    print("refresh ->", r.status_code)
    new = r.json()
    assert new.get("access_token"), new
    tok.update(new)
    json.dump(tok, open(tokfile, "w"))


async def call(base, tokfile, tool, args):
    tok = json.load(open(tokfile))["access_token"]
    t0 = time.monotonic()
    async with streamablehttp_client(f"{base}/mcp", headers={"Authorization": f"Bearer {tok}"}) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool(tool, args)
            out = "\n".join(getattr(x, "text", "") for x in res.content)
    print(f"[{tool} {time.monotonic() - t0:.2f}s]")
    print(out[:1500])


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "login":
        login(sys.argv[2].rstrip("/"), sys.argv[3], sys.argv[4])
    elif cmd == "refresh":
        refresh(sys.argv[2].rstrip("/"), sys.argv[3])
    elif cmd == "call":
        asyncio.run(call(sys.argv[2].rstrip("/"), sys.argv[3], sys.argv[4], json.loads(sys.argv[5])))
