"""GitHub App authentication, REST helpers, OAuth login and webhook signature checks."""
import base64
import hashlib
import hmac
import os
import time
from datetime import datetime
from urllib.parse import urlencode

import httpx
import jwt

from . import ORG

API = "https://api.github.com"


def verify_signature(secret, body, header):
    """True when X-Hub-Signature-256 matches the body signed with the webhook secret."""
    if not (secret and header and header.startswith("sha256=")):
        return False
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, header[7:])


class GitHub:
    """Installation-token client for the GitHub App. GITHUB_TOKEN (a plain token) works for local dev."""

    def __init__(self):
        self.app_id = os.environ.get("GITHUB_APP_ID", "")
        key_file = os.environ.get("GITHUB_APP_PRIVATE_KEY_FILE", "")
        self.key = (open(key_file).read() if os.path.isfile(key_file)
                    else os.environ.get("GITHUB_APP_PRIVATE_KEY", "").replace("\\n", "\n"))
        self.static = os.environ.get("GITHUB_TOKEN", "")
        self.client_id = os.environ.get("GITHUB_CLIENT_ID", "")
        self.client_secret = os.environ.get("GITHUB_CLIENT_SECRET", "")
        self._token, self._expires = "", 0.0
        self.http = httpx.Client(base_url=API, timeout=30, follow_redirects=False,
                                 headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})

    @property
    def enabled(self):
        return bool(self.static or (self.app_id and self.key))

    @property
    def oauth_enabled(self):
        return bool(self.client_id and self.client_secret)

    def token(self):
        if self.static:
            return self.static
        if time.time() < self._expires - 120:
            return self._token
        now = int(time.time())
        app_jwt = jwt.encode({"iat": now - 60, "exp": now + 540, "iss": self.app_id}, self.key, algorithm="RS256")
        auth = {"Authorization": f"Bearer {app_jwt}"}
        inst = self.http.get(f"/orgs/{ORG}/installation", headers=auth)
        inst.raise_for_status()
        r = self.http.post(f"/app/installations/{inst.json()['id']}/access_tokens", headers=auth)
        r.raise_for_status()
        self._token = r.json()["token"]
        self._expires = datetime.fromisoformat(r.json()["expires_at"].replace("Z", "+00:00")).timestamp()
        return self._token

    def request(self, method, path, check=True, **kw):
        r = self.http.request(method, path, headers={"Authorization": f"Bearer {self.token()}"}, **kw)
        if check:
            r.raise_for_status()
        return r

    def get(self, path, **params):
        return self.request("GET", path, params=params).json()

    def paginate(self, path, key=None, limit=1000, **params):
        """Follow Link: rel=next. key picks the list out of wrapped responses like check-runs."""
        params.setdefault("per_page", 100)
        out, url, first = [], path, True
        while url and len(out) < limit:
            r = self.request("GET", url, params=params if first else None)
            data = r.json()
            out += data[key] if key else data
            url, first = r.links.get("next", {}).get("url"), False
        return out[:limit]

    def git_auth(self):
        """git -c args that send the installation token, so it never lands in .git/config."""
        if not self.enabled:
            return []  # offline/local mode: whatever credentials git already has
        basic = base64.b64encode(f"x-access-token:{self.token()}".encode()).decode()
        return ["-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {basic}"]

    def is_member(self, login):
        r = self.request("GET", f"/orgs/{ORG}/members/{login}", check=False)
        return r.status_code == 204

    # ---- "Sign in with GitHub" (the App's user-authorization flow) ----

    def oauth_url(self, state, redirect_uri):
        return "https://github.com/login/oauth/authorize?" + urlencode(
            {"client_id": self.client_id, "redirect_uri": redirect_uri, "state": state})

    def oauth_login(self, code, redirect_uri):
        r = httpx.post("https://github.com/login/oauth/access_token", timeout=30, headers={"Accept": "application/json"},
                       data={"client_id": self.client_id, "client_secret": self.client_secret,
                             "code": code, "redirect_uri": redirect_uri})
        r.raise_for_status()
        user_token = r.json().get("access_token")
        if not user_token:
            raise PermissionError(r.json().get("error_description", "GitHub refused the login"))
        me = httpx.get(f"{API}/user", timeout=30, headers={"Authorization": f"Bearer {user_token}"})
        me.raise_for_status()
        return me.json()["login"]


gh = GitHub()
