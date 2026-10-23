# GG-Docs ↔ GG-Ops — "Connect workspace" for every user

**Status:** Proposal, revised after a feasibility check · **Date:** 2026-10-23 · **Owners:** GG-Ops team, GG-Docs team

**Checked against:** GG-Ops `chore/optimization-pass` (08b0812) · GG-Docs `re/frontend` (64a33a8)

## Summary

Today each GG-Docs workspace stores **one GG-Ops API key**, a personal key an admin pasted into Settings. Everyone in that GG-Docs workspace reads linked GG-Ops projects through it, whether or not they are a member in GG-Ops.

This proposal instead lets a GG-Ops user click **Open in GG-Docs** inside their workspace, approve once, and land in GG-Docs connected **as themselves**. GG-Docs then sees exactly what that person can see in GG-Ops, nothing more.

GG-Ops already runs a full OAuth 2.1 authorization server (built for MCP connectors such as claude.ai). It covers dynamic client registration, a consent screen, PKCE, rotating refresh tokens, and revocation. GG-Docs becomes another OAuth client of it.

**What connecting does not do:** it gives GG-Docs a person's GG-Ops view. It doesn't make them a member of a GG-Docs workspace. GG-Docs membership still comes from a GG-Docs invite (see [Who can use it](#who-can-use-it)).

**Effort:**

| Side | Work |
|---|---|
| GG-Ops | Small. A button, a `/api/v1/me` endpoint, one env var, and a token rate-limit exception before wide rollout. The OAuth server is reused as-is. |
| GG-Docs | Medium. An OAuth client, mostly copied from the existing GitHub sign-in (PKCE, encrypted per-user tokens, refresh). Also new: a strict refresh lock, a "Projects" view, and the mapping between a GG-Ops workspace and a GG-Docs workspace. |

---

## What the user sees

```
 GG-Ops: workspace "Acme"                GG-Ops consent (first time only)          GG-Docs
┌──────────────────────────┐            ┌──────────────────────────────────┐     ┌──────────────────────┐
│ Sidebar                  │  click     │ GG-Docs wants to access your     │     │ Acme                 │
│  …                       │ ─────────► │ GGOps account                    │ ──► │  ✓ Connected as      │
│  [↗ Open in GG-Docs]     │            │  • Read your tasks, projects,    │     │    alice@acme.com    │
└──────────────────────────┘            │    and workspaces                │     │  Projects: …         │
                                        │ You'll be sent to gg-docs.app    │     └──────────────────────┘
                                        │      [Authorize]   [Deny]        │
                                        └──────────────────────────────────┘
```

- **First click:** sign in to GG-Docs with GitHub if needed, then the GG-Ops consent screen, then GG-Docs opens.
- **Every later click:** goes straight into GG-Docs, with no consent and no sign-in, as long as the GG-Docs session is alive. GG-Docs already holds the person's tokens, so it doesn't re-run the flow.
- **Disconnect:** either in GG-Docs (Settings → Disconnect GG-Ops) or in GG-Ops (Account → API keys → revoke **"GG-Docs (OAuth)"**).

The **Projects** panel is new GG-Docs UI. Today GG-Docs only shows linked projects' names and statuses in Settings.

---

## Why per-user OAuth instead of the shared key

| | Workspace key (today) | Per-user OAuth (this proposal) |
|---|---|---|
| Who can connect | One admin per GG-Docs workspace | Any GG-Ops user who is a GG-Docs member, with a button |
| What GG-Docs can read | Every workspace the key's owner is in | Only what *that person* can see |
| GG-Docs viewer with no GG-Ops access | Still sees linked projects | Sees nothing from GG-Ops |
| Revoking access | Kills it for the whole GG-Docs workspace | Per person |
| Key leak | Exposes everything the admin can see, until revoked | Exposes one person's view, and expires in 1 h |
| GG-Ops code needed | None | Small (see below) |

**The workspace key stays** for callers that act as no particular person: GG-Docs API clients with the `workflow:read` scope. Anything **shown to a person** should use that person's token.

---

## How it works

```
 Alice (browser)          GG-Ops                                   GG-Docs server
 ───────────────          ──────                                   ──────────────
 1. clicks "Open in GG-Docs"
    └─ link: {GGDOCS_URL}/connect/ggops?workspace=acme ─────────────────────► 2. Alice signed in to GG-Docs?
                                                                                  no  → GitHub sign-in, then back
                                                                                        here with ?workspace kept
                                                                                  Alice already connected?
                                                                                  yes → jump to step 7
                                                                                  no  → make PKCE verifier + state,
 3. ◄────────── 302 /oauth/authorize?client_id=…&redirect_uri=…                        keep them in the GG-Docs session
                &code_challenge=…&code_challenge_method=S256
                &scope=read&state=…
 4. GG-Ops: signed in already (else /sign-in?next=…), shows consent
    Alice clicks Authorize
 5. ── 302 {redirect_uri}?code=…&state=…&iss=… ──────────────────────────► 6. check state and iss, then
                                                                                 POST /oauth/token
                                     ◄──────────── code + code_verifier ───────  (form-encoded)
                                     ── access_token (1 h) + refresh_token ──►   store both encrypted, per user
                                     ◄──────────── GET /api/v1/me ────────────   remember GG-Ops user id
                                     ◄──────────── GET /api/v1/workspaces ────   is "acme" in the list?
 7. ◄──────────────────────────────────────────────────────────────────────────  open GG-Docs, showing Acme's projects
```

`redirect_uri` is `{GGDOCS_URL}/connect/ggops/callback`.

Later reads use `Authorization: Bearer <access_token>`, the same as an API key. GG-Docs refreshes the token **shortly before it expires** (the same way it handles GitHub tokens), not after a 401. Every refresh also **revokes the previous access token** in GG-Ops, which is why refreshes must be serialized (GG-Docs work, step 6).

---

## What GG-Ops already has (no work needed)

Paths are under `my-app/src/`.

| Piece | Where |
|---|---|
| Discovery metadata | `GET /.well-known/oauth-authorization-server` |
| Client registration (RFC 7591, public clients, PKCE only) | `POST /oauth/register` → `modules/oauth/server/actions/register-client.ts` |
| Consent screen, which shows the redirect origin and sends signed-out users to sign-in | `app/oauth/authorize/page.tsx` |
| Return to the consent screen after sign-in (`?next=` checked to stay on GG-Ops) | `modules/auth/next-url.ts` (on `chore/optimization-pass`) |
| `iss` on the callback (RFC 9207), equal to `APP_URL` without a trailing slash | `modules/oauth/server/actions/authorize.ts` |
| Code and refresh exchange, with rotation and reuse detection. Reuse revokes the whole grant, and every refresh revokes the previous access token. | `POST /oauth/token` → `modules/oauth/server/actions/exchange-token.ts` |
| Revoke (RFC 7009) | `POST /oauth/revoke` |
| Access token is an `ApiKey` row (`kind: "OAUTH"`, 1 h, `ggops_sk_` prefix), so every `/api/v1` route accepts it | `lib/api-key.ts`, `modules/auth/server/session.ts` |
| Read-only scope blocks writes | `lib/api/handler.ts` (403 on non-GET without `write`) |
| Access ends on password change or "sign out everywhere" | `authVersion` check in `loadActiveUser` |
| Revoke from the UI | Account → API keys. Revoking an OAuth key also kills its refresh grant (`revoke-api-key.ts`). |
| Rate limits (enforced when Upstash is set, which production requires) | `lib/rate-limit.ts`; 600 requests/hour per access token |

---

## GG-Ops work

### Required

1. **Merge `chore/optimization-pass`.** On `main`, the sign-in page follows `?next=` without checking it, which is an open redirect. The branch's `safeNextUrl` fixes that and still returns people to `/oauth/authorize?…`.

2. **Register GG-Docs as a client.** This is one setup call, not code:
   ```bash
   curl -X POST https://<ggops>/oauth/register -H 'content-type: application/json' \
     -d '{"client_name":"GG-Docs","redirect_uris":["https://<gg-docs>/connect/ggops/callback"]}'
   ```
   Give the returned `client_id` to the GG-Docs team. The redirect URI must be `https` (plain `http` only works for localhost). A GG-Docs install reached by IP over `http` can't use this until it has a domain with HTTPS.

3. **`GGDOCS_URL` env var.** Make it optional in `src/lib/env.ts`. When it's unset, the button is hidden.

4. **"Open in GG-Docs" button** in the workspace sidebar (`app/(dashboard)/w/[workspaceSlug]/sidebar-nav.tsx`). It's a plain link:
   `${GGDOCS_URL}/connect/ggops?workspace=${slug}`. GG-Ops does nothing else, because GG-Docs drives the OAuth flow.
   `sidebar-nav.tsx` is a client component and `lib/env.ts` is server-only, so read `GGDOCS_URL` in the server layout and pass it in as a prop (or name it `NEXT_PUBLIC_GGDOCS_URL`).

5. **`GET /api/v1/me`** returns `{ id, name, email }` for the token's user. It's about 10 lines using `handleAuthed`. GG-Docs needs it to know which GG-Ops account just connected. Add it to `modules/api-docs/spec.ts`. (`Docs/API-V1-CONTRACT.md` doesn't exist; the spec is the contract.)

6. **Token and revoke rate limits.** `POST /oauth/token` and `POST /oauth/revoke` each allow **60 requests/hour per IP**, and both fail closed. Every GG-Docs user's connect, refresh and disconnect calls come from GG-Docs's server IP, so they share those budgets. That's fine below about 50 active users an hour. Before a wider rollout, give GG-Docs's fixed server IPs a higher limit (for example an `OAUTH_TOKEN_IP_ALLOWLIST` env var in `app/oauth/token/route.ts` and `app/oauth/revoke/route.ts`).
   **Caveat:** `clientIp()` reads the *first* `X-Forwarded-For` value. Vercel overwrites that header, so an allowlist is safe there. Behind CloudFront (the planned Amplify move), the first value comes from the caller, so anyone could claim GG-Docs's IP. On Amplify, the allowlist must read the address the platform appends instead.

### Optional (later)

- **"Open in GG-Docs" on the project header.** Link to `…/connect/ggops?workspace=<slug>&project=<projectId>`. This also covers the "Copy project ID" request.
- **Skip consent for a trusted GG-Docs client.** Today the consent screen shows every time GG-Docs re-runs the flow. Skip it only for a `client_id` set in env, never based on `client_name`, because anyone can register a client called "GG-Docs".
- **Connected apps page** (Account → Connected apps) listing OAuth grants by app instead of mixing them into API keys. Each consent creates a new grant, so reconnecting without revoking first leaves extra **"GG-Docs (OAuth)"** rows. GG-Docs revokes the old grant on reconnect (GG-Docs work, step 5), so this is cosmetic.
- **Workspace-bound tokens.** Today a token can read *every* workspace the user belongs to, and `?workspace=` is only a hint. Hard binding would need a `workspaceId` on `OAuthGrant` and `ApiKey`, plus a check in `handleAuthed`. Only build this if a user asks to share one workspace but not another.
- **Contract test** for the `{ ok, data, error }` envelope and the board fields GG-Docs reads. `modules/api-docs/wire-schemas.ts` already defines them, so the test can parse a board against that schema.

---

## GG-Docs work

GG-Docs already does this whole dance for GitHub sign-in. Copy it instead of writing a new client:

| Need | Existing code to copy |
|---|---|
| PKCE verifier and challenge | `pkce()` in `portal/github.py` |
| State and verifier kept across the redirect | `request.session["oauth"]` in `/auth/github` (`portal/main.py`). The session is a signed, SameSite=Lax cookie, which the browser sends on GG-Ops's redirect back. No separate cookie is needed. |
| Tokens encrypted per user | `save_github_tokens` in `portal/db.py`, with `secret.seal` |
| Refresh shortly before expiry | `user_token` in `portal/main.py` (refreshes when under 60 s are left) |

1. **Config:** `GGOPS_URL`, `GGOPS_CLIENT_ID`. The callback is `{PUBLIC_URL}/connect/ggops/callback`. This assumes one GG-Ops instance. The per-workspace GG-Ops address in Settings stays for the workspace key only.

2. **Storage:** a new control collection, for example `ggops_connections`, keyed by GG-Docs `user_id` (a connection belongs to a person, not a workspace, so it isn't tenant data). It holds the GG-Ops user id and email, the sealed access and refresh tokens, `expires_at` and `connected_at`.

3. **`/connect/ggops` route:** requires a GG-Docs session.
   - Add `/connect/` to `NO_WORKSPACE` in `portal/main.py`. Otherwise a person with no GG-Docs workspace is sent to `/welcome/` and the flow is lost.
   - The login redirect in `require_login` keeps only the path (`next={quote(path)}`). Include the query string so `?workspace=` survives GitHub sign-in.
   - If the user is already connected, open GG-Docs (see step 9). Otherwise:
     - generate a PKCE `code_verifier` (S256) and a `state`;
     - store both in the session under their own key (for example `ggops_oauth`), with a timestamp, as `/auth/github` does;
     - redirect to `{GGOPS_URL}/oauth/authorize` with `scope=read`.

4. **Callback route, `/connect/ggops/callback`:** keep it out of `/api/`, where a signed-out request gets a JSON 401 instead of a sign-in redirect.
   - pop the saved state, and check `state`, its age, and that `iss` equals `GGOPS_URL` without a trailing slash;
   - on `error=access_denied`, show "not connected";
   - POST the code to `/oauth/token` as `application/x-www-form-urlencoded` with `grant_type`, `code`, `redirect_uri`, `client_id` and `code_verifier`;
   - store the tokens encrypted for this user;
   - call `/api/v1/me` and `/api/v1/workspaces`.

5. **Reconnect:** if the user already had a connection, revoke its refresh token (`POST /oauth/revoke`) before saving the new one. Otherwise the old grant stays alive in GG-Ops for up to 90 days.

6. **Refresh with a strict lock.** Refresh tokens rotate on every use, and each refresh revokes the previous access token. If two requests refresh with the same token at once, the second looks like a replay, and GG-Ops **revokes the whole connection**.
   - Refresh when under 60 s are left, like `user_token`, not after a 401.
   - Take a per-user lock. GG-Docs runs as one uvicorn process today, so a `threading.Lock` per user is enough. Switch to a MongoDB lease if it ever runs more processes.
   - **After taking the lock, re-read the stored tokens.** If another request already refreshed, use its new access token and don't refresh again.
   - Save the new refresh token before using the new access token.
   - **Send each refresh once.** Retry only when GG-Ops answered with 429 or 5xx, because then nothing rotated. Never retry after a timeout or a dropped connection: GG-Ops may have rotated already, and the retry would revoke the connection. `integrations.call()` retries and only does GET, so it can't be reused for this.

7. **Per-user reads.**
   - Fetch boards with the viewing user's token. Never serve one user's cached board to another user.
   - Per-user reads must **not** update a workflow's stored status. `check_workflow` writes that status for the whole GG-Docs workspace, so one non-member's 404 would mark the project "not found" for everyone. Only the admin's "verify" check should write it.
   - Use the person's token wherever a person reads live GG-Ops data: `GET /api/v1/integrations/workflows/{id}` when a person calls it, and the new Projects view. API clients keep using the workspace key.
   - The workflow names in Settings were saved when an admin linked them, so every GG-Docs member still sees them. Decide whether that's acceptable, or hide them from people whose own token can't read the project.

8. **Projects view (new UI):** shows the projects of the GG-Ops workspace named by `?workspace=`, read from `/api/v1/workspaces/{slug}/projects` with the person's token.

9. **Which GG-Docs workspace to open.** `?workspace=acme` is a GG-Ops slug. GG-Docs workspaces are numbered and have no link to a GG-Ops workspace.
   - **Phase 1:** open the person's current GG-Docs workspace, and use the slug only to pick which GG-Ops projects the Projects view shows.
   - **Later, if needed:** let an admin store a GG-Ops workspace slug on the GG-Docs workspace, and switch to the matching one.

10. **Errors:**

    | Response | GG-Docs should |
    |---|---|
    | 401 on an API call | Refresh once (under the lock). If it still fails, show "Reconnect GG-Ops". |
    | `invalid_grant` on refresh | Revoked, expired after 90 days unused, replayed, or the password changed. Show "Reconnect GG-Ops". |
    | 429 from `/oauth/token` | Rate limited; the body says `invalid_request`, so go by the status. Nothing rotated, so keep the stored tokens and retry later. |
    | Timeout or dropped connection on refresh | Don't retry (step 6). The next request uses the stored token; if GG-Ops had rotated, that means "Reconnect GG-Ops". |
    | 404 | No access, or the project is archived. |
    | 400 | Malformed project ID. |
    | 429 on an API call | Rate limited (600 requests/hour per token). Retry later. |

11. **Disconnect:** `POST {GGOPS_URL}/oauth/revoke` with `token=<refresh_token>`, then delete the stored tokens. If revoke fails (for example a 429), delete locally anyway, and tell the person they can also revoke it under GG-Ops → Account → API keys.

---

## Who can use it

Connecting GG-Ops tells GG-Docs who someone is in GG-Ops. It doesn't let them into a GG-Docs workspace, which holds private repository code, docs and reviews.

- **Phase 1:** only people who are already GG-Docs members can use the button. Anyone else signs in, lands on GG-Docs's welcome page, and needs an invite as today.
- **Auto-joining** (for example, "members of GG-Ops workspace *acme* join GG-Docs workspace *Acme*") needs the workspace mapping from GG-Docs work step 9, plus an explicit decision that GG-Ops membership should grant access to code. It's not part of this proposal.

---

## Security notes

- **Read-only.** Request `scope=read`. Writing (for example creating tasks from review findings) is a separate decision with its own consent wording.
- **No client secret.** GG-Ops only issues public clients, so PKCE is the proof. GG-Docs must never skip it.
- **Check `iss` on the callback** (RFC 9207). This blocks mix-ups if GG-Docs ever talks to more than one OAuth server.
- **The consent screen shows the redirect origin.** That's the real trust signal, because any client can register under the name "GG-Docs".
- **Never retry a refresh blindly.** A replayed refresh token revokes the whole connection by design.
- **Connecting grants nothing in GG-Docs.** GG-Docs access still comes from GG-Docs membership.
- **Blast radius:** an access token is one person's read access, valid for 1 hour, and rate-limited to 600 requests/hour per token.

---

## Sign-in: what "automatic" means

GG-Docs signs people in with GitHub, and this flow **connects** accounts. It doesn't replace GG-Docs sign-in.

- **Phase 1 (this doc):** the first time, the user signs in to GG-Docs with GitHub, then approves GG-Ops. After that, the button goes straight in.
- **Phase 2 (GG-Docs only, optional): "Sign in with GG-Ops".** The same OAuth round trip plus `/api/v1/me` gives GG-Docs a verified GG-Ops identity, so GG-Docs could create or find an account from it. GG-Ops needs nothing beyond what's listed above. On the GG-Docs side it's medium work, not a small add-on:
  - GitHub is only needed by admins who import repositories. Reading docs and reviews and using chat don't need it.
  - GG-Docs users and memberships are keyed by GitHub `login`, and `claim_pending` (`portal/db.py`) gives waiting memberships to whoever signs in with a matching login. An account made from GG-Ops needs a login in its own namespace (for example `ggops:<id>`) and must skip `claim_pending`, or it could claim memberships meant for a GitHub user.
  - The members API, the audit log and role changes all name people by `login`, so they need checking with the new namespace.

---

## Rollout

| Step | Owner | Done when |
|---|---|---|
| 1. Merge `chore/optimization-pass`, register the client, add `GGDOCS_URL` | GG-Ops | `client_id` is shared with GG-Docs |
| 2. `/api/v1/me` and the sidebar button | GG-Ops | Button visible; `/me` returns the caller |
| 3. Connect, callback, storage and locked refresh | GG-Docs | A test user connects and sees their workspaces |
| 4. Per-user reads and the Projects view | GG-Docs | A non-member viewer sees nothing for a linked project, and stored workflow status is unchanged by their reads |
| 5. Token and revoke rate-limit allowlist | GG-Ops | Done before more than ~50 active GG-Docs users |
| 6. (Optional) Workspace mapping, project button, skip consent, Sign in with GG-Ops | Both | — |

## Test checklist

- [ ] New user: button → consent → lands in GG-Docs showing that workspace's projects.
- [ ] Signed out of GG-Docs: button → GitHub sign-in → back on `/connect/ggops` with `?workspace=` kept.
- [ ] Signed out of GG-Ops: consent → GG-Ops sign-in → back on the consent screen.
- [ ] GG-Docs user with no GG-Docs workspace: the flow finishes instead of bouncing to `/welcome/` halfway.
- [ ] Second click: no consent, straight in.
- [ ] Deny on consent → GG-Docs shows "not connected" (`error=access_denied`).
- [ ] Reconnect → only one **"GG-Docs (OAuth)"** row left in GG-Ops Account → API keys.
- [ ] Revoke in GG-Ops Account → API keys → GG-Docs shows "Reconnect" on its next read.
- [ ] Change password in GG-Ops → same result.
- [ ] User removed from a workspace → that workspace's projects return 404 in GG-Docs.
- [ ] Two parallel GG-Docs requests at token expiry → connection survives, and only one refresh reaches GG-Ops.
- [ ] Refresh that times out → not retried.
- [ ] A GG-Docs user who never connected can't see GG-Ops data for a linked project.
- [ ] A non-member's read doesn't change a workflow's status in Settings for anyone else.
- [ ] API clients with `workflow:read` still work (workspace key).

## Open questions

1. **What is GG-Docs's public URL?** It's GG-Docs's `PUBLIC_URL`, needed for `GGDOCS_URL` and the redirect URI. It must be HTTPS on a domain, not an IP over `http`.
2. **Should GG-Ops membership ever grant GG-Docs membership?** Phase 1 says no. Yes would need the workspace mapping and a security sign-off, because GG-Docs holds private code.
3. **Where will GG-Ops run in production, Vercel or Amplify?** This decides how the rate-limit allowlist reads the caller's IP.

### Answered by the feasibility check

- **Does GG-Docs need GitHub for its own features?** Only for admins importing repositories. Phase 2 can come later, but it needs the login namespacing above.
- **Is a workspace hint enough, or must a connection be hard-limited to one workspace?** The hint is enough until someone asks for more.
- **Should the shared key stay?** Yes, for GG-Docs API clients with `workflow:read`, which act as no person. Drop it only if those clients lose that scope.
