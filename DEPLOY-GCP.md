# Deploying the mvp branch to a Google Cloud VM

For a Compute Engine VM you already reach over SSH. Everything on the VM is plain `ssh` and `scp`; the few
Google Cloud settings are clicks in the Console. `install.sh` does the actual setup: Docker, `.env`, HTTPS, and the
stack (portal, MongoDB, Caddy).

Below, `user@VM_IP` is how you SSH in today, and `VM_IP` is the VM's external IP.

## 1. Check the VM (Console, once)

**Size.** Ubuntu 22.04/24.04 or Debian 12, **8 GB RAM at least** (16 GB recommended, and needed for a local model),
**40-60 GB disk**. Check with `free -g` and `df -h /` over SSH. To resize: Compute Engine → VM instances → the VM
→ **Stop** → **Edit** → Machine type (`e2-standard-2` is 8 GB, `e2-standard-4` is 16 GB) → Save → **Start**.

**A static IP.** The HTTPS address and the GitHub App's URLs contain the IP, so it must never change.
VPC network → **IP addresses** → the VM's external address → **Promote to static IP address**.

**Ports 80 and 443 open.** Compute Engine → VM instances → the VM → **Edit** → Firewalls: tick **Allow HTTP
traffic** and **Allow HTTPS traffic** → Save. Port 80 must be open too: Let's Encrypt uses it to issue the
certificate. Leave 7000 closed; `install.sh` binds it to localhost and Caddy serves it over 443.

## 2. Get `mvp` onto the VM

**Either push it** once you've reviewed it, then clone that branch on the VM:

```bash
# on your machine
git push -u origin mvp
```

```bash
# on the VM
git clone -b mvp https://github.com/CR-8/gg-docs.git ~/gg-portal
```

If the repository is private, give the VM a read-only deploy key first and clone over SSH (README 3.2.0, step 3),
with `-b mvp` added.

**Or copy it up without pushing.** A bundle is the branch and its history in one file:

```bash
# on your machine, in the repository
git bundle create ~/mvp.bundle mvp
scp ~/mvp.bundle user@VM_IP:~/mvp.bundle
```

```bash
# on the VM
git clone -b mvp ~/mvp.bundle ~/gg-portal
```

To update later, make a new bundle and `scp` it over `~/mvp.bundle` again; `install.sh` pulls from it (step 6).

## 3. Install

```bash
# on the VM
cd ~/gg-portal
bash install.sh VM_IP
```

Use the `install.sh` inside this checkout. A copy of the script kept anywhere else, or one piped from `curl`, clones
`main` instead of `mvp`.

It installs Docker and writes `.env`. The address becomes
`https://<ip-with-dashes>.sslip.io`, a free name that points at your IP, so no domain is needed. It also generates
the session secret, encryption key and webhook secret. Then it builds and starts everything
and waits for HTTPS. The first build takes several minutes. At the end it prints the portal's address and the exact
URLs for the GitHub App.

Afterwards:

- Log out and SSH in again once, so `docker compose` works without `sudo`.
- Copy `~/gg-portal/.env` somewhere safe, like a password manager. Its `TOKEN_ENCRYPTION_KEY` is needed to read the
  stored GitHub tokens and API keys.

## 4. The GitHub App, and optionally your own domain

1. Create the GitHub App (README 3.1) with the URLs `install.sh` printed, and install it on the repositories you
   want.
2. Copy its private key up:
   ```bash
   # on your machine
   scp gg-reviewer.*.private-key.pem user@VM_IP:~/gg-portal/app.pem
   ```
3. Re-run with the App's values, then delete the copy (the script moved it into `secrets/`):
   ```bash
   # on the VM
   cd ~/gg-portal
   GITHUB_APP_ID=... GITHUB_CLIENT_ID=... GITHUB_CLIENT_SECRET=... APP_KEY=~/gg-portal/app.pem bash install.sh VM_IP
   rm ~/gg-portal/app.pem
   ```

**Your own domain instead of sslip.io:** at your DNS provider, add an A record pointing the name at `VM_IP`. Once it
resolves (`dig +short portal.example.com`), run `DOMAIN=portal.example.com bash install.sh VM_IP` and change the
GitHub App's four URLs to the new name.

**Email invites** (optional): pass `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD` and `SMTP_FROM` to `install.sh`. Google
Cloud blocks outgoing port 25, so use your provider's port 587 (the default) or 465.

**A local model** (optional, 16 GB VM): `LOCAL_LLM=auto bash install.sh VM_IP` (README 3.2). Otherwise pick a hosted
model in Settings after signing in.

## 5. First run

Open the portal's address and follow README 3.3: **Continue with GitHub**, create a workspace, import repositories,
then Settings → LLM (Test connection, then Save). The first sync loads 90 days of history and writes the docs;
Settings → Recent jobs shows its progress.

Check that it's healthy:

```bash
curl -s https://<your-address>/healthz        # {"ok": true, "database": "up"}
```

In the GitHub App, Advanced → **Recent deliveries** should show `200` for the ping.

## 6. Updating

```bash
# on the VM
cd ~/gg-portal && bash install.sh VM_IP
```

It pulls the branch (from GitHub, or from a fresh `~/mvp.bundle`), keeps everything already in `.env`, rebuilds and
restarts. The quicker equivalent is `git pull && docker compose up -d --build`.

## 7. Backups

**Disk snapshots** cover everything on the VM (database, clones, `.env`): Compute Engine → **Snapshots**
→ Snapshot schedules → **Create** (daily, keep 14 days). Then Compute Engine → **Disks** → the VM's disk → **Edit**
→ Snapshot schedule → pick it → Save.

**A database dump** to keep on your own machine:

```bash
# on the VM
cd ~/gg-portal && docker compose exec -T mongo mongodump --archive --gzip --db gg-docs > ~/gg-docs-$(date +%F).archive.gz
```

```bash
# on your machine, with the date of that dump
scp user@VM_IP:~/gg-docs-2026-10-24.archive.gz .
```

Keep the `.env` copy from step 3 apart from these.

## 8. If an older release already runs on this VM

- **Its data:** the release before workspaces kept everything in SQLite. Back up the `portal_data` volume, then
  migrate it (README 6.1). The portal refuses to start until that's done.
- **A single-tenant install:** set `GITHUB_ORG`, `BACKEND_REPO` and `FRONTEND_REPO` in `.env` to that install's real
  values *before* migrating. On `mvp` they default to `your-org`, `your-org/backend` and `your-org/frontend`, which
  would name the workspace and its repositories wrongly.

## 9. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `install.sh` ends with "Not reachable over HTTPS yet" | Port 80 or 443 is closed: tick both firewall boxes on the VM (step 1). Then `docker compose logs caddy` |
| The certificate fails for your own domain | DNS doesn't point at `VM_IP` yet (`dig +short portal.example.com`), or port 80 is closed |
| The address changed after a restart | The external IP was ephemeral: promote it to static (step 1) and re-run `install.sh` |
| The build is killed, or the VM freezes | Not enough memory: 8 GB at least, 16 GB with a local model (step 1) |
| `permission denied` on `docker` | Log out and SSH in again after the first install (step 3) |
| Invites never arrive | Port 25 is blocked on Google Cloud: use port 587 or 465 |
| Anything else | README 6, Troubleshooting |
