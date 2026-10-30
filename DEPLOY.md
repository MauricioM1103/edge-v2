# Deploying Edge v2.0

Three ways to run Edge, from simplest to most polished.

---

## 1. Desktop app (double-click)

The fastest path. No cloud, no account, all data local.

**Option A — frameless window** (requires Chrome or Edge; recommended):
```
double-click Edge-DesktopApp.pyw
```
Launches a chromeless Chrome/Edge window pointed at your local Streamlit
server. Closes the server when you close the window. No terminal visible.

**Option B — browser + console**:
```
double-click Edge.bat
```
Starts the server in a visible terminal and opens your default browser.

Either way: data stays on this machine, API keys sit in
`.streamlit/secrets.toml`, nothing is uploaded.

---

## 2. Streamlit Community Cloud (free, public URL)

Permanent public URL at `https://your-app.streamlit.app`.
Streamlit Cloud is free for public apps; the dashboard supports access
restriction via Google sign-in if you want it private.

**Steps:**

1. Create a GitHub repo:
   ```
   cd betting-dashboard-v2
   git init
   git add -A
   git commit -m "Edge v2.0 initial commit"
   gh repo create edge-v2 --public --source=. --push
   ```
   (or create a repo manually at github.com and `git remote add` + `push`.)

2. Visit https://share.streamlit.io and click **New app**.

3. Pick your repo, branch `main`, main file `app.py`.

4. In the Advanced settings, add your secrets:
   ```toml
   the_odds_api_key = "xxxxxxxx"
   kalshi_access_key_id = "xxxxxxxx"
   kalshi_private_key_pem = """-----BEGIN PRIVATE KEY-----
   ...
   -----END PRIVATE KEY-----"""
   # OR, to just hard-code a balance without live auth:
   kalshi_balance_usd = "1234.56"
   ```

5. Click **Deploy**. First build takes ~90 seconds, redeploys on every push
   to `main` are automatic.

---

## 3. Share on your LAN (no cloud)

When Streamlit is running on this machine, it binds to all interfaces.
Any device on the same Wi-Fi can hit it directly:

```
http://<your-ip>:8601
```

The LAN URL is printed when you run `streamlit run app.py` — look for the
**Network URL** line. On this machine right now it's `http://192.168.68.51:8601`.

---

## Secrets reference

Place in `.streamlit/secrets.toml` locally (or in the Streamlit Cloud UI for
cloud deploys):

```toml
# The Odds API - needed for live lines, EV, Kelly, agent
the_odds_api_key = "YOUR_KEY"

# Kalshi - either live auth, OR a static manual balance
kalshi_access_key_id = "..."
kalshi_private_key_pem = """-----BEGIN PRIVATE KEY-----
...
-----END PRIVATE KEY-----"""
# OR
kalshi_balance_usd = "1234.56"
```

Equivalent environment variables (useful for the .bat launcher):
- `THE_ODDS_API_KEY`
- `KALSHI_ACCESS_KEY_ID`
- `KALSHI_PRIVATE_KEY_PATH` (path to a .pem file)
- `KALSHI_BALANCE_USD` (manual override)

---

## Windows shortcut on the desktop

Right-click `Edge-DesktopApp.pyw` → **Send to** → **Desktop (create shortcut)**.
Rename the shortcut "Edge". Optionally set a custom icon via Properties →
Change Icon.
