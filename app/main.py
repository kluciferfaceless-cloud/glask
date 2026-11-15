"""Glask web server: Discord sign-in, role-based access per guild, shared storage."""
import json, os, secrets, time
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware

from .store import Store

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
                     officer=ids('MISCHIEF_OFFICER_ROLES'), recruit=ids('MISCHIEF_RECRUIT_ROLES')),
    'Teek': dict(discord=os.environ.get('TEEK_DISCORD_ID', ''),
                 officer=ids('TEEK_OFFICER_ROLES'), recruit=ids('TEEK_RECRUIT_ROLES')),
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
                    + ('<p><a href="/dev-login" style="color:#a8957d">Dev sign-in</a></p>' if DEV_LOGIN else ''))
    allowed = [g for g in DATA['guilds'] if g['server'] in u['access']]
    if not allowed:
        return page(f"<p>Signed in as <b>{u['name']}</b>, but Glask doesn't recognize an officer or recruitment role "
                    "for you in The Faceless or Faceless Fury Discord.</p><p><a href='/logout' style='color:#a8957d'>Sign out</a></p>")
    data = dict(DATA, guilds=allowed)
    kv = {k: v for k, v in store.all().items() if can(u, k, write=False)}
    me = dict(id=u['id'], name=u['name'], avatar=u.get('avatar'), access=u['access'])
    safe = lambda o: json.dumps(o).replace('</', '<\\/')
    html = (TEMPLATE.replace('__DATA__', safe(data)).replace('__MASK__', MASK)
            .replace('__ME__', safe(me)).replace('__STORE__', safe(kv)))
    return HTMLResponse(html, headers={'Cache-Control': 'no-store'})


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
        access = {}
        for server, cfg in GUILDS.items():
            if me['id'] in ADMINS:
                access[server] = 'officer'
                continue
            if not cfg['discord']:
                continue
            r = await c.get(f"{DISCORD_API}/users/@me/guilds/{cfg['discord']}/member", headers=h)
            if r.status_code != 200:
                continue                                   # not in that Discord server
            roles = set(r.json().get('roles', []))
            if roles & cfg['officer']:
                access[server] = 'officer'
            elif roles & cfg['recruit']:
                access[server] = 'recruit'
    name = me.get('global_name') or me.get('username')
    av = f"https://cdn.discordapp.com/avatars/{me['id']}/{me['avatar']}.png?size=64" if me.get('avatar') else None
    # Roles are re-checked at each sign-in; sessions last 12 hours so role changes take effect the same day.
    request.session['user'] = dict(id=me['id'], name=name, avatar=av, access=access, exp=time.time() + 12 * 3600)
    store.audit(f"{name} ({me['id']})", 'sign-in', '', json.dumps(access))
    return RedirectResponse('/')


@app.get('/dev-login')
def dev_login(request: Request, level: str = 'officer'):
    if not DEV_LOGIN:
        raise HTTPException(404)
    request.session['user'] = dict(id='dev', name='Dev ' + level, avatar=None, exp=time.time() + 3600,
                                   access={'Mischief': level, 'Teek': level})
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


@app.get('/api/audit')
def audit(request: Request):
    u = user(request)
    if not u or 'officer' not in u['access'].values():
        raise HTTPException(403)
    return JSONResponse(store.recent_audit())


@app.get('/healthz')
def health():
    return {'ok': True}
