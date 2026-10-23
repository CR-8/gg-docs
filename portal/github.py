"""GitHub App authentication (an installation token per account that installed the App), REST helpers,
"Sign in with GitHub" (the App's user tokens, refreshed when they expire) and webhook signature checks."""
import base64
import hashlib
import hmac
import os
import re
import secrets
import time
from datetime import datetime
from urllib.parse import urlencode

import httpx
import jwt

API = "https://api.github.com"
REPO_PATH = re.compile(r"/repos/([^/]+/[^/?]+)")


class RateLimited(Exception):
    """GitHub's rate limit is used up; retry_after is in seconds."""

    def __init__(self, retry_after):
        super().__init__(f"GitHub's API rate limit is used up; try again in {retry_after} s")
        self.retry_after = retry_after


def pkce():
    """(verifier, S256 challenge) for the OAuth code exchange (RFC 7636)."""
    verifier = secrets.token_urlsafe(48)
    return verifier, base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def repo_meta(r, login=None):
    """What the portal shows and keeps about a repository; `access` (with the user's login) tells owned repositories
    from organization ones and ones the user collaborates on."""
    owner = r["owner"]
    out = {"github_id": r["id"], "owner": owner["login"], "description": r.get("description"),
           "visibility": r.get("visibility") or ("private" if r.get("private") else "public"),
           "private": bool(r.get("private")), "default_branch": r.get("default_branch"), "html_url": r.get("html_url")}
    if login is not None:
        out["access"] = ("owner" if owner["login"].lower() == login.lower() else
                         "organization" if owner.get("type") == "Organization" else "collaborator")
    return out


def verify_signature(secret, body, header):
    """True when X-Hub-Signature-256 matches the body signed with the webhook secret."""
    if not (secret and header and header.startswith("sha256=")):
        return False
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, header[7:])


class GitHub:
    """Installation tokens for the GitHub App: each repository is read with the token of the installation that
    covers it. GITHUB_TOKEN (a plain token) works for local dev and single-operator setups."""

    def __init__(self):
        self.app_id = os.environ.get("GITHUB_APP_ID", "")
        key_file = os.environ.get("GITHUB_APP_PRIVATE_KEY_FILE", "")
        self.key = (open(key_file).read() if os.path.isfile(key_file)
                    else os.environ.get("GITHUB_APP_PRIVATE_KEY", "").replace("\\n", "\n"))
        self.static = os.environ.get("GITHUB_TOKEN", "")
        self.client_id = os.environ.get("GITHUB_CLIENT_ID", "")
        self.client_secret = os.environ.get("GITHUB_CLIENT_SECRET", "")
        self._installs, self._tokens, self._app_url = {}, {}, None  # repo -> installation id -> (token, expires)
        self.http = httpx.Client(base_url=API, timeout=30, follow_redirects=False,
                                 headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})

    @property
    def enabled(self):
        return bool(self.static or (self.app_id and self.key))

    @property
    def mode(self):
        return "app" if self.app_id and self.key else "token" if self.static else None

    @property
    def oauth_enabled(self):
        return bool(self.client_id and self.client_secret)

    def app_auth(self):
        now = int(time.time())
        app_jwt = jwt.encode({"iat": now - 60, "exp": now + 540, "iss": self.app_id}, self.key, algorithm="RS256")
        return {"Authorization": f"Bearer {app_jwt}"}

    def installation(self, repo):
        """Id of the App installation that can read repo, or None when the App is not installed on it."""
        if repo not in self._installs:
            r = self.http.get(f"/repos/{repo}/installation", headers=self.app_auth())
            if r.status_code == 404:
                return None
            r.raise_for_status()
            self._installs[repo] = r.json()["id"]
        return self._installs[repo]

    def token(self, repo):
        if self.mode == "token":  # the App wins when both are set
            return self.static
        inst = self.installation(repo)
        if not inst:
            raise PermissionError(f"the GitHub App is not installed on {repo}")
        token, expires = self._tokens.get(inst, ("", 0.0))
        if time.time() < expires - 120:
            return token
        r = self.http.post(f"/app/installations/{inst}/access_tokens", headers=self.app_auth())
        if r.status_code == 404:  # uninstalled since: look it up again next time
            self._installs.pop(repo, None)
        r.raise_for_status()
        token = r.json()["token"]
        self._tokens[inst] = token, datetime.fromisoformat(r.json()["expires_at"].replace("Z", "+00:00")).timestamp()
        return token

    def _token_for(self, path, token):
        if token:
            return token
        m = REPO_PATH.match(path)
        if not m:
            raise ValueError(f"{path} names no repository: pass a user token")
        return self.token(m[1])

    def request(self, method, path, check=True, token=None, **kw):
        """token: a user's token; by default the installation token of the repository in the path."""
        r = self.http.request(method, path, headers={"Authorization": f"Bearer {self._token_for(path, token)}"}, **kw)
        if r.status_code in (403, 429) and (r.headers.get("retry-after") or r.headers.get("x-ratelimit-remaining") == "0"):
            reset = int(r.headers.get("x-ratelimit-reset") or 0)
            raise RateLimited(int(r.headers.get("retry-after") or max(1, reset - int(time.time()))))
        if check:
            r.raise_for_status()
        return r

    def get(self, path, token=None, **params):
        return self.request("GET", path, token=token, params=params).json()

    def paginate(self, path, key=None, limit=1000, token=None, **params):
        """Follow Link: rel=next. key picks the list out of wrapped responses like check-runs. The next links
        name repositories by id, so the first page decides the token."""
        params.setdefault("per_page", 100)
        token = self._token_for(path, token)
        out, url, first = [], path, True
        while url and len(out) < limit:
            r = self.request("GET", url, token=token, params=params if first else None)
            data = r.json()
            out += data[key] if key else data
            url, first = r.links.get("next", {}).get("url"), False
        return out[:limit]

    def git_auth(self, repo):
        """git -c args that send the installation token, so it never lands in .git/config."""
        if not self.enabled:
            return []  # offline/local mode: whatever credentials git already has
        basic = base64.b64encode(f"x-access-token:{self.token(repo)}".encode()).decode()
        return ["-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {basic}"]

    def install_url(self):
        """Where people install the App on more accounts or repositories (it then sends their webhooks)."""
        if self._app_url is None:
            r = self.http.get("/app", headers=self.app_auth())
            r.raise_for_status()
            self._app_url = r.json()["html_url"]
        return f"{self._app_url}/installations/new"

    def user_repos(self, token, login):
        """Repositories this user can read through the App's installations (every page of each): what Import lists,
        and the accounts the App is installed on. GitHub shows a user only the installations they can reach and, in
        each, only the repositories they can read: someone whose own account hasn't installed the App sees just what
        others share with them. A repository two installations cover is listed once."""
        out, accounts = {}, set()
        for inst in self.paginate("/user/installations", key="installations", limit=100, token=token):
            if account := (inst.get("account") or {}).get("login"):
                accounts.add(account)
            for r in self.paginate(f"/user/installations/{inst['id']}/repositories", key="repositories",
                                   limit=2000, token=token):
                out[r["id"]] = {"repo": r["full_name"], "pushed_at": r.get("pushed_at"), **repo_meta(r, login)}
        return list(out.values()), sorted(accounts, key=str.lower)

    # ---- "Sign in with GitHub" (the App's user-authorization flow) ----

    def oauth_url(self, state, redirect_uri, challenge):
        """No scope parameter: a GitHub App's user token carries the App's permissions, limited to the repositories
        where the App is installed and the user has access. Installing the App is the separate consent step."""
        return "https://github.com/login/oauth/authorize?" + urlencode(
            {"client_id": self.client_id, "redirect_uri": redirect_uri, "state": state,
             "code_challenge": challenge, "code_challenge_method": "S256"})

    def _user_token(self, **grant):
        r = httpx.post("https://github.com/login/oauth/access_token", timeout=30, headers={"Accept": "application/json"},
                       data={"client_id": self.client_id, "client_secret": self.client_secret, **grant})
        r.raise_for_status()
        t = r.json()
        if not t.get("access_token"):
            raise PermissionError(t.get("error_description", "GitHub refused the login"))
        # Expiring user tokens (the App default) last 8 hours and come with a refresh token.
        return {"token": t["access_token"], "refresh_token": t.get("refresh_token"),
                "token_expires": time.time() + t["expires_in"] if t.get("expires_in") else None}

    def oauth_login(self, code, redirect_uri, verifier):
        """The signed-in user's profile (github_id: GitHub's immutable numeric id) and tokens."""
        tokens = self._user_token(code=code, redirect_uri=redirect_uri, code_verifier=verifier)
        me = self.get("/user", token=tokens["token"])
        return {"github_id": me["id"], "login": me["login"], "name": me.get("name"), "avatar_url": me.get("avatar_url"),
                **tokens}

    def refresh(self, refresh_token):
        return self._user_token(grant_type="refresh_token", refresh_token=refresh_token)

    def revoke_grant(self, token):
        """Revokes the user's authorization of the App on GitHub (and with it every token it issued them)."""
        r = httpx.request("DELETE", f"{API}/applications/{self.client_id}/grant", auth=(self.client_id, self.client_secret),
                          json={"access_token": token}, timeout=30,
                          headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
        return r.status_code in (204, 404)  # 404: already revoked


gh = GitHub()
