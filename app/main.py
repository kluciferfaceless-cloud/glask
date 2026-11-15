"""Glask web server: Discord sign-in, role-based access per guild, shared storage."""
import json, os, secrets, threading, time
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware

from .store import Store
from . import live_loot, item_slots

ROOT = Path(__file__).resolve().parent.parent
DISCORD_API = 'https://discord.com/api/v10'
CLIENT_ID = os.environ.get('DISCORD_CLIENT_ID', '')
CLIENT_SECRET = os.environ.get('DISCORD_CLIENT_SECRET', '')
BASE_URL = os.environ.get('BASE_URL', 'http://localhost:8000').rstrip('/')
DEV_LOGIN = os.environ.get('DEV_LOGIN') == '1'   # local testing only, never set on Railway


def ids(name):
    return {x.strip() for x in os.environ.get(name, '').split(',') if x.strip()}


# Each Glask guild (by server name in the data) maps to one Discord server and its roles.
GUILDS = {
    'Mischief': dict(discord=os.environ.get('MISCHIEF_DISCORD_ID', ''),
                     officer=ids('MISCHIEF_OFFICER_ROLES'), recruit=ids('MISCHIEF_RECRUIT_ROLES'),
                     leader=ids('MISCHIEF_LEADER_ROLES') or {'1085933966843973643'}),        # Leadership
    'Teek': dict(discord=os.environ.get('TEEK_DISCORD_ID', ''),
                 officer=ids('TEEK_OFFICER_ROLES'), recruit=ids('TEEK_RECRUIT_ROLES'),
                 leader=ids('TEEK_LEADER_ROLES') or {'1114320364470812683'}),               # Leadership
}
ADMINS = ids('ADMIN_DISCORD_USER_IDS')   # always officer on every guild (e.g. Klucifer)

# What each access level may change. Keys look like glask-<kind>-<Server>[-...].
RECRUIT_WRITE = {'inv', 'ach', 'att', 'raids'}          # uploads that feed applicant tracking
OFFICER_ONLY_READ = {'pip'}                              # PIPs are hidden from the recruitment team

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=os.environ.get('SESSION_SECRET') or secrets.token_hex(32),
                   https_only=BASE_URL.startswith('https'), same_site='lax', max_age=14 * 24 * 3600)
store = Store()
TEMPLATE = (ROOT / 'app' / 'template.html').read_text()
MASK = (ROOT / 'app' / 'mask.b64').read_text().strip()
DATA = json.loads((ROOT / 'data' / 'glask.json').read_text())
AA = json.loads((ROOT / 'data' / 'aa.json').read_text())


def current_xpac(server, now=None):
    """Which expansion a guild's server is in right now, from Daybreak's unlock calendar, plus the AA scale for it."""
    from datetime import datetime, timezone
    now = now or datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    sched = AA['schedule'].get(server, [])
    past = [s for s in sched if s[1] <= now]
    if not past:
        return None
    code, since = past[-1]
    nxt = next((s for s in sched if s[1] > now), None)
    order = AA['order']
    # autogrant comes from the most recent unlock that granted AA
    ag_code = None
    for c, _ in past:
        if c in AA['autogrant_through']:
            ag_code = AA['autogrant_through'][c]
    zero = {k: 0 for k in AA['cumulative'][order[0]]}
    return dict(code=code, xpac=AA['names'][code], since=since,
                ag_through=AA['names'].get(ag_code, 'nothing yet'), ag=AA['eligible'][ag_code] if ag_code else zero,
                max=AA['cumulative'][code], next=dict(xpac=AA['names'][nxt[0]], at=nxt[1]) if nxt else None)


def user(request):
    u = request.session.get('user')
    if not u or u.get('exp', 0) < time.time():
        return None
    return u


def key_parts(key):
    p = key.split('-', 3)
    if len(p) < 3 or p[0] != 'glask':
        return None, None
    return p[1], p[2]


def can(u, key, write):
    kind, scope = key_parts(key)
    if kind is None:
        return False
    if kind == 'logsettings':
        return scope == u['id']                       # personal setting
    if kind == 'slots':
        return bool(u['access']) and not write          # shared item -> slot list; changed through /api/slots
    if kind == 'qcount':
        return any(a == 'officer' for a in u['access'].values())
    level = u['access'].get(scope)
    if not level:
        return False
    if level == 'officer':
        return True
    if write:
        return kind in RECRUIT_WRITE
    return kind not in OFFICER_ONLY_READ


def page(body, title='Glask'):
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><link rel="icon" type="image/png" href="data:image/png;base64,{MASK}">
<style>body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#120d0b;color:#e8dccb;font:16px system-ui,sans-serif}}
.box{{text-align:center;max-width:420px;padding:32px}}img{{width:110px}}h1{{letter-spacing:.2em;margin:12px 0 4px}}
small{{color:#a8957d}}p{{color:#c9b9a3;line-height:1.5}}a.btn{{display:inline-block;margin-top:18px;background:#5865F2;color:#fff;
padding:12px 22px;border-radius:8px;text-decoration:none;font-weight:600}}</style></head>
<body><div class="box"><img src="data:image/png;base64,{MASK}" alt=""><h1>GLASK</h1><small>Harbinger of the Faceless</small>{body}</div></body></html>""")


@app.get('/', response_class=HTMLResponse)
def index(request: Request):
    u = user(request)
    if not u:
        return page('<p>The Faceless sees all. Sign in to continue.</p><a class="btn" href="/login">Sign in with Discord</a>'
                    + '<p style="font-size:13px"><a href="/privacy" style="color:#a8957d">Privacy policy</a> · <a href="/terms" style="color:#a8957d">Terms of use</a></p>'
                    + ('<p><a href="/dev-login" style="color:#a8957d">Dev sign-in</a></p>' if DEV_LOGIN else ''))
    allowed = [g for g in DATA['guilds'] if g['server'] in u['access']]
    if not allowed:
        return page(f"<p>Signed in as <b>{u['name']}</b>, but Glask doesn't recognize an officer or recruitment role "
                    "for you in The Faceless or Faceless Fury Discord.</p><p><a href='/logout' style='color:#a8957d'>Sign out</a></p>")
    data = dict(DATA, guilds=allowed)
    data['loot_status'] = {g['server']: live_loot.STATUS.get(g['server']) for g in allowed}
    data['aa'] = dict(buffer=AA['buffer'], **{g['server']: current_xpac(g['server']) for g in allowed})
    kv = {k: v for k, v in store.all().items() if can(u, k, write=False)}
    for k in [k for k in kv if k.startswith('glask-uploads-')]:
        kv[k] = uploads_for(u, k.split('-', 2)[2], kv[k])
    me = dict(id=u['id'], name=u['name'], avatar=u.get('avatar'), access=u['access'], lead=u.get('lead', []))
    safe = lambda o: json.dumps(o).replace('</', '<\\/')
    html = (TEMPLATE.replace('__DATA__', safe(data)).replace('__MASK__', MASK)
            .replace('__ME__', safe(me)).replace('__STORE__', safe(kv)))
    return HTMLResponse(html, headers={'Cache-Control': 'no-store'})


LEGAL_CSS = ("<style>body{margin:0;background:#120d0b;color:#e8dccb;font:16px/1.6 system-ui,sans-serif}"
             "main{max-width:760px;margin:0 auto;padding:40px 22px 80px}h1{letter-spacing:.08em}h2{margin-top:32px;font-size:19px;color:#f0d9a8}"
             "a{color:#e0704f}small{color:#a8957d}li{margin:4px 0}</style>")
UPDATED = 'November 15, 2026'


def legal(title, body):
    return HTMLResponse(f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
                        f"<title>{title} · Glask</title>{LEGAL_CSS}</head><body><main><h1>{title}</h1><small>Last updated {UPDATED}</small>{body}"
                        "<p style='margin-top:40px'><a href='/privacy'>Privacy policy</a> · <a href='/terms'>Terms of use</a> · <a href='/'>Glask</a></p></main></body></html>")


@app.get('/privacy', response_class=HTMLResponse)
def privacy():
    return legal('Privacy policy', """
<p>Glask is a guild management tool run by Klucifer, Guild Leader of The Faceless (Mischief), for the officers and recruitment team of
The Faceless and Faceless Fury, two EverQuest guilds. It is a volunteer, non-commercial project and is not affiliated with Daybreak Games or Discord.</p>

<h2>What Glask gets from Discord when you sign in</h2>
<ul><li>Your Discord user ID, username or display name, and avatar.</li>
<li>Your roles in The Faceless and Faceless Fury Discord servers, only so Glask can decide what you're allowed to see. Glask does not see your
roles in any other server.</li></ul>
<p>Glask asks Discord only for the <code>identify</code> and <code>guilds.members.read</code> permissions. It does not get your email address, your
messages, your friends list or your list of servers, and it never posts anything or joins a server as you. Discord's sign-in token is used once at
sign-in to read the information above and is not stored.</p>

<h2>What Glask keeps about you as a user</h2>
<ul><li>A sign-in cookie in your browser that holds your Discord ID, name, avatar and access level. It expires after 12 hours or when you sign out.</li>
<li>A change log: when you sign in, and what you save or change (for example uploads, notes, checklist items and votes), recorded with your Discord name and ID.
Notes, votes and uploads you make are shown to other officers with your name on them.</li></ul>

<h2>Guild information Glask shows to officers</h2>
<p>To do its job, Glask displays information about guild members and applicants to signed-in officers and, in part, the recruitment team:</p>
<ul><li>In-game guild roster details: character names, levels, classes, ranks, last-online dates, zones and guild notes.</li>
<li>Loot records from the guild loot sheets, and raid performance and attendance parsed from raid logs that officers upload.</li>
<li>Answers given on the guild application forms, including Discord handles, and launch-planning questionnaire answers.</li>
<li>Inventory and achievement files that officers or members upload.</li>
<li>Officer notes, recruitment votes and decisions, and performance improvement plans.</li></ul>
<p>Access is limited by Discord role: officers see their own guild's information; the recruitment team sees a smaller set and cannot see
performance improvement plans. People without those roles cannot see anything.</p>

<h2>Where it's stored and who else is involved</h2>
<ul><li>Glask runs on Railway (railway.com), which hosts the app and its database in the United States.</li>
<li>Glask reads the guild's Google Sheets (loot, applications, questionnaires) through a read-only Google service account.</li>
<li>Discord handles sign-in.</li></ul>
<p>Nothing is sold, shared with advertisers or used to train AI models. There is no advertising or tracking in Glask.</p>

<h2>Keeping and deleting information</h2>
<p>Information stays in Glask while it's useful to the guild. You can ask Klucifer (in either guild Discord) to see what Glask holds about you, to correct it,
or to delete it. Removing someone from the guild's Discord roles removes their access at their next sign-in. You can also revoke Glask at any time in
Discord under User Settings → Authorized Apps.</p>

<h2>Changes</h2>
<p>If this policy changes, the date at the top changes, and meaningful changes are announced in the guild Discords.</p>""")


@app.get('/terms', response_class=HTMLResponse)
def terms():
    return legal('Terms of use', """
<p>These terms cover using Glask, the guild management tool for The Faceless and Faceless Fury. By signing in you agree to them.</p>
<h2>Who can use it</h2>
<p>Glask is for officers and recruitment team members of The Faceless and Faceless Fury. Access comes from your Discord roles and ends when those roles do.
It is currently a pilot, so features and data may change or be reset.</p>
<h2>Using it responsibly</h2>
<ul><li>Information in Glask about members and applicants is for running the guild. Don't copy it out to share publicly or with people outside guild leadership.</li>
<li>Keep notes, votes and plans professional and about in-game conduct and performance.</li>
<li>Only upload files you have the right to share, such as your own logs or files members have given you for this purpose.</li>
<li>Don't try to access parts of Glask your role doesn't allow, or interfere with how it runs.</li></ul>
<h2>No warranty</h2>
<p>Glask is a free, volunteer-run tool provided as-is. It may have bugs, be unavailable, or show incomplete information, so check anything important
before acting on it. To the extent the law allows, the people who run Glask aren't liable for losses from using it.</p>
<h2>Not affiliated</h2>
<p>EverQuest is a trademark of Daybreak Game Company. Glask is not affiliated with or endorsed by Daybreak Games or Discord.</p>
<h2>Changes and contact</h2>
<p>These terms may be updated; the date at the top will change. Questions go to Klucifer in either guild Discord.</p>""")


@app.get('/login')
def login(request: Request):
    if not CLIENT_ID:
        raise HTTPException(500, 'DISCORD_CLIENT_ID is not set')
    state = secrets.token_urlsafe(24)
    request.session['state'] = state
    q = urlencode(dict(client_id=CLIENT_ID, redirect_uri=BASE_URL + '/auth/callback', response_type='code',
                       scope='identify guilds.members.read', state=state, prompt='none'))
    return RedirectResponse('https://discord.com/oauth2/authorize?' + q)


@app.get('/auth/callback')
async def callback(request: Request, code: str = '', state: str = ''):
    if not code or state != request.session.pop('state', None):
        return RedirectResponse('/')
    async with httpx.AsyncClient(timeout=15) as c:
        tok = await c.post(DISCORD_API + '/oauth2/token', data=dict(
            grant_type='authorization_code', code=code, redirect_uri=BASE_URL + '/auth/callback'),
            auth=(CLIENT_ID, CLIENT_SECRET))
        if tok.status_code != 200:
            return page('<p>Discord sign-in failed. Try again.</p><a class="btn" href="/login">Sign in with Discord</a>')
        h = {'Authorization': 'Bearer ' + tok.json()['access_token']}
        me = (await c.get(DISCORD_API + '/users/@me', headers=h)).json()
        access = {}; lead = []
        for server, cfg in GUILDS.items():
            if me['id'] in ADMINS:
                access[server] = 'officer'; lead.append(server)
                continue
            if not cfg['discord']:
                continue
            r = await c.get(f"{DISCORD_API}/users/@me/guilds/{cfg['discord']}/member", headers=h)
            if r.status_code != 200:
                continue                                   # not in that Discord server
            roles = set(r.json().get('roles', []))
            if roles & cfg['leader']:
                lead.append(server)
            if roles & (cfg['officer'] | cfg['leader']):
                access[server] = 'officer'
            elif roles & cfg['recruit']:
                access[server] = 'recruit'
    name = me.get('global_name') or me.get('username')
    av = f"https://cdn.discordapp.com/avatars/{me['id']}/{me['avatar']}.png?size=64" if me.get('avatar') else None
    # Roles are re-checked at each sign-in; sessions last 12 hours so role changes take effect the same day.
    request.session['user'] = dict(id=me['id'], name=name, avatar=av, access=access, lead=lead, exp=time.time() + 12 * 3600)
    store.audit(f"{name} ({me['id']})", 'sign-in', '', json.dumps(access))
    return RedirectResponse('/')


@app.get('/dev-login')
def dev_login(request: Request, level: str = 'officer'):
    if not DEV_LOGIN:
        raise HTTPException(404)
    request.session['user'] = dict(id='dev-' + level, name='Dev ' + level, avatar=None, exp=time.time() + 3600,
                                   access={'Mischief': 'officer' if level == 'leader' else level, 'Teek': 'officer' if level == 'leader' else level},
                                   lead=['Mischief', 'Teek'] if level == 'leader' else [])
    return RedirectResponse('/')


@app.get('/logout')
def logout(request: Request):
    request.session.clear()
    return RedirectResponse('/')


@app.post('/api/kv')
async def put_kv(request: Request):
    u = user(request)
    if not u:
        raise HTTPException(401, 'Sign in again')
    body = await request.json()
    key, value = body.get('key', ''), body.get('value')
    if not can(u, key, write=True):
        raise HTTPException(403, "You don't have permission to change that")
    if len(json.dumps(value)) > 8_000_000:
        raise HTTPException(413, 'Too large')
    store.put(key, value, f"{u['name']} ({u['id']})")
    return {'ok': True}


def uploads_for(u, server, lst):
    if server in u.get('lead', []):
        return lst
    return [x for x in lst if x.get('uid') == u['id'] or (not x.get('uid') and x.get('by') == u['name'])]


SLOT_NAMES = {'Head', 'Face', 'Ear', 'Neck', 'Shoulders', 'Arms', 'Back', 'Wrist', 'Range', 'Hands', 'Primary', 'Secondary',
              'Fingers', 'Chest', 'Legs', 'Feet', 'Waist', 'Charm', 'Power Source', 'Not gear'}


@app.post('/api/slots')
async def set_slots(request: Request):
    """Teach Glask which slot items go in: from uploaded inventories (anyone) or an officer tagging an item."""
    u = user(request)
    if not u or not u['access']:
        raise HTTPException(401, 'Sign in again')
    b = await request.json()
    items = {str(k)[:120]: v for k, v in (b.get('items') or {}).items() if v in SLOT_NAMES or v is None}
    manual = bool(b.get('manual'))
    if manual and 'officer' not in u['access'].values():
        raise HTTPException(403, 'Only officers can tag item slots')
    with REC_LOCK:
        cur = store.get('glask-slots-all') or {}
        for k, v in items.items():
            if v is None:
                cur.pop(k, None)
            elif manual or k not in cur:     # inventories never overwrite an officer's tag
                cur[k] = v
        store.put('glask-slots-all', cur, f"{u['name']} ({u['id']})")
    return cur


@app.post('/api/raid-delete')
async def raid_delete(request: Request):
    """Leadership only: remove a raid log (and the attendance it added) so a bad upload stops affecting the data."""
    u = user(request)
    b = await request.json()
    server, rid, date = b.get('server', ''), b.get('id', ''), b.get('date', '')
    if not u or server not in u.get('lead', []):
        raise HTTPException(403, 'Only Leadership can delete raid logs')
    who = f"{u['name']} ({u['id']})"
    with REC_LOCK:
        rk, ak, hk = 'glask-raids-' + server, 'glask-att-' + server, 'glask-hidden-' + server
        raids = store.get(rk) or []
        keep = [r for r in raids if not (r.get('id') == rid and r.get('date') == date)]
        hidden = store.get(hk) or []
        if len(keep) != len(raids):
            att = {}
            for r in keep:   # attendance from uploads is rebuilt from the raid logs that remain
                att[r['date']] = sorted({p['main'] for p in r['players'] if p.get('main')} | set(att.get(r['date'], [])))
            store.put(rk, keep, who)
            store.put(ak, att, who)
        else:
            if f'{rid}|{date}' not in hidden:   # a raid that came with Glask's starting data: hide it
                hidden.append(f'{rid}|{date}')
                store.put(hk, hidden, who)
        store.audit(who, 'delete raid', rk, f'{rid} {date}')
    return {'raids': store.get(rk) or [], 'att': store.get(ak) or {}, 'hidden': store.get(hk) or []}


@app.post('/api/upload-log')
async def upload_log(request: Request):
    u = user(request)
    if not u:
        raise HTTPException(401, 'Sign in again')
    b = await request.json()
    server = b.get('server', '')
    if server not in u['access']:
        raise HTTPException(403, "You don't have access to that guild")
    key = 'glask-uploads-' + server
    lst = store.get(key) or []
    lst.insert(0, dict(at=time.time(), by=u['name'], uid=u['id'], file=str(b.get('file', ''))[:200],
                       kind=str(b.get('kind', ''))[:40], msg=str(b.get('msg', ''))[:300]))
    lst = lst[:200]
    store.put(key, lst, f"{u['name']} ({u['id']})")
    return {'server': server, 'list': uploads_for(u, server, lst)}


REC_LOCK = threading.Lock()
STAGES = ['new', 'claimed', 'interview', 'trial', 'decision', 'promoted', 'declined', 'withdrew']
CHECKS = ['welcome', 'discord', 'requirements', 'dkp', 'raidbuilder', 'thread']


@app.post('/api/rec')
async def rec(request: Request):
    """One change to an applicant's recruitment record, merged on the server so officers never overwrite each other."""
    u = user(request)
    if not u:
        raise HTTPException(401, 'Sign in again')
    b = await request.json()
    server, rid, op = b.get('server', ''), str(b.get('id', '')).lower()[:60], b.get('op', '')
    level = u['access'].get(server)
    if not level or not rid:
        raise HTTPException(403, "You don't have access to that guild")
    officer = level == 'officer'
    who = u['name']
    now = time.time()
    with REC_LOCK:
        key = 'glask-rec-' + server
        recs = store.get(key) or {}
        r = recs.setdefault(rid, dict(name=b.get('name', rid), notes=[], votes={}, checks={}, history=[]))
        if op == 'stage':
            st = b.get('stage')
            if st not in STAGES:
                raise HTTPException(400, 'Unknown stage')
            if st in ('promoted', 'declined') and not officer:
                raise HTTPException(403, 'Only officers can make the final decision')
            r['stage'] = st
            r['history'].append(dict(at=now, by=who, text='Moved to ' + st))
            if st in ('promoted', 'declined', 'withdrew'):
                r['decision'] = dict(result=st, by=who, at=now, why=str(b.get('why', ''))[:500])
        elif op == 'claim':
            r['officer'] = who if not b.get('release') else None
            if not r.get('stage') or r['stage'] == 'new':
                r['stage'] = 'claimed'
            r['history'].append(dict(at=now, by=who, text='Released' if b.get('release') else 'Claimed'))
        elif op == 'field':
            f = b.get('field')
            if f not in ('recruited_by', 'contact', 'sme'):
                raise HTTPException(400, 'Unknown field')
            r[f] = str(b.get('value', ''))[:200]
        elif op == 'check':
            c = b.get('check')
            if c not in CHECKS:
                raise HTTPException(400, 'Unknown checklist item')
            r['checks'][c] = dict(by=who, at=now) if b.get('value') else None
        elif op == 'note':
            text = str(b.get('text', '')).strip()[:2000]
            if text:
                r['notes'].append(dict(at=now, by=who, text=text, kind=b.get('kind', 'note') if b.get('kind') in ('note', 'sme') else 'note'))
        elif op == 'vote':
            if not officer:
                raise HTTPException(403, 'Only officers vote')
            v = b.get('vote')
            if v not in ('promote', 'extend', 'decline', None):
                raise HTTPException(400, 'Unknown vote')
            if v:
                r['votes'][u['id']] = dict(name=who, vote=v, at=now, why=str(b.get('why', ''))[:500])
            else:
                r['votes'].pop(u['id'], None)
        else:
            raise HTTPException(400, 'Unknown change')
        r['updated'] = now
        store.put(key, recs, f"{who} ({u['id']})")
    return {'server': server, 'recs': recs}


@app.post('/api/rec-settings')
async def rec_settings(request: Request):
    u = user(request)
    b = await request.json()
    server = b.get('server', '')
    if not u or u['access'].get(server) != 'officer':
        raise HTTPException(403, 'Only officers can change requirements')
    v = dict(level=int(b.get('level') or 0), aa=int(b.get('aa') or 0), raids=int(b.get('raids') or 6))
    store.put('glask-recset-' + server, v, f"{u['name']} ({u['id']})")
    return v


@app.on_event('startup')
def _start_live_loot():
    live_loot.start(DATA, AA)
    item_slots.start(store, AA)


@app.post('/api/loot-refresh')
def loot_refresh(request: Request):
    u = user(request)
    if not u or 'officer' not in u['access'].values():
        raise HTTPException(403, 'Only officers can refresh')
    servers = [s for s, lvl in u['access'].items() if lvl == 'officer']
    return live_loot.refresh(DATA, AA, servers)


@app.get('/api/audit')
def audit(request: Request):
    u = user(request)
    if not u or not u.get('lead'):
        raise HTTPException(403)
    return JSONResponse(store.recent_audit())


@app.get('/healthz')
def health():
    return {'ok': True}
