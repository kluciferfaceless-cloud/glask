# Glask

Harbinger of the Faceless: guild management for The Faceless (Mischief) and Faceless Fury (Teek).
Not DKP or attendance; those live elsewhere.

## How it works

- `app/main.py` – FastAPI server. Sign in with Discord; access comes from Discord roles.
- `app/template.html` – the Glask front end. The server fills in the guild data, the signed-in user and the shared saved data.
- `app/store.py` – shared storage (Postgres on Railway, SQLite locally) plus an audit log of who changed what.
- `data/glask.json` – the guild dataset built by `pipeline/` from guild dumps, loot sheets, applications, logs and exports.

## Access

| Level | Who | Sees | Can change |
|---|---|---|---|
| Officer | The Faceless: Raid Lead, Management, Leadership. Faceless Fury: Leadership, Raid Leaders, DKP Team | Everything for their guild | Everything for their guild |
| Member | Anyone else in either Discord | Their own character page only: gear, achievements, AA, own parse vs anonymous class averages, launch sign-up, uploads for their own files | Their own inventory and achievement files, raid logs |
| Recruitment | Recruitment Team in either Discord | Overview, Roster, Gear, Achievements, Applications, Uploads (no PIPs, Cleanup, Performance, Launch planning) | Inventories, achievement exports, raid logs, attendance |

Each person only sees the guild(s) where they hold a role. Roles are checked at every sign-in (sessions last 12 hours).

## Railway environment variables

| Variable | Value |
|---|---|
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` (reference to the Postgres service) |
| `BASE_URL` | The app's public address, e.g. `https://glask-production.up.railway.app` |
| `SESSION_SECRET` | Any long random string |
| `DISCORD_CLIENT_ID` | From the Discord developer portal |
| `DISCORD_CLIENT_SECRET` | From the Discord developer portal (never commit this) |
| `MISCHIEF_DISCORD_ID` | The Faceless Discord server ID |
| `MISCHIEF_OFFICER_ROLES` | Comma-separated role IDs: Raid Lead, Management, Leadership |
| `MISCHIEF_RECRUIT_ROLES` | Recruitment Team role ID |
| `TEEK_DISCORD_ID` | Faceless Fury Discord server ID |
| `TEEK_OFFICER_ROLES` | Comma-separated role IDs: Leadership, Raid Leaders, DKP Team |
| `TEEK_RECRUIT_ROLES` | Recruitment Team role ID |
| `ADMIN_DISCORD_USER_IDS` | Optional: Discord user IDs that are officers everywhere |

In the Discord developer portal, add `BASE_URL` + `/auth/callback` under OAuth2 → Redirects.

## Run locally

```
pip install -r requirements.txt
DEV_LOGIN=1 uvicorn app.main:app --reload
```
Open http://localhost:8000 and use "Dev sign-in" (`/dev-login?level=recruit` to test the recruitment view).
Never set `DEV_LOGIN` on Railway.
