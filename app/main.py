"""Glask web server: Discord sign-in, role-based access per guild, shared storage."""
import json, os, re, secrets, threading, time
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware

from .store import Store
from . import live_loot, itemdb

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
                     leader=ids('MISCHIEF_LEADER_ROLES') or {'1085933966843973643'},        # Leadership
                     raidlead=ids('MISCHIEF_RAIDLEAD_ROLES')),
    'Teek': dict(discord=os.environ.get('TEEK_DISCORD_ID', ''),
                 officer=ids('TEEK_OFFICER_ROLES'), recruit=ids('TEEK_RECRUIT_ROLES'),
                 leader=ids('TEEK_LEADER_ROLES') or {'1114320364470812683'},               # Leadership
                 raidlead=ids('TEEK_RAIDLEAD_ROLES') or {'1244648980948717639'}),         # Raid Leaders
}
ADMINS = ids('ADMIN_DISCORD_USER_IDS')   # always officer on every guild (e.g. Klucifer)
# Temporary officer access by Discord username, one guild at a time (must also be in that guild's Discord). Remove when done.
TEMP_OFFICERS = {'Mischief': set(), 'Teek': set()}

# What each access level may change. Keys look like glask-<kind>-<Server>[-...].
RECRUIT_WRITE = {'inv', 'ach'}                           # uploads that feed applicant tracking (raid logs go through /api/raid-add)
OFFICER_ONLY_READ = {'pip', 'raidpend', 'raidsrc', 'raidchk', 'lootrev', 'loothist'}   # PIPs, raid-log review and loot history are officers only       # PIPs and raid-log review are hidden from the recruitment team

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=os.environ.get('SESSION_SECRET') or secrets.token_hex(32),
                   https_only=BASE_URL.startswith('https'), same_site='lax', max_age=14 * 24 * 3600)
store = Store()
TEMPLATE = (ROOT / 'app' / 'template.html').read_text()
MASK = (ROOT / 'app' / 'mask.b64').read_text().strip()
DATA = json.loads((ROOT / 'data' / 'glask.json').read_text())
AA = json.loads((ROOT / 'data' / 'aa.json').read_text())
FLAGS = {k: v for k, v in json.loads((ROOT / 'data' / 'flags.json').read_text()).items() if not k.startswith('_')}


def _strings(o):
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for v in o.values():
            yield from _strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from _strings(v)


_AMB = json.loads((ROOT / 'data' / 'ach_ambig.json').read_text())
ACH_AMBIG, ACH_MULTI = _AMB['names'], _AMB.get('multi_raid', [])
FLAG_NAMES = {code: {n.lower() for n in _strings(t)} for code, t in FLAGS.items()}


def future_ach(server):
    """Achievement names from expansions the server hasn't unlocked yet (only a test-server log or export can have these)."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    out = set()
    for code, at in AA['schedule'].get(server, []):
        if at > now:
            out |= FLAG_NAMES.get(code, set())
    return out


def strip_ach_export(v, drop):
    """An achievement export with the given (lowercased) achievement names taken out."""
    if not isinstance(v, dict) or not drop:
        return v
    v = dict(v)
    for f in ('done', 'raidDone', 'raidNames'):
        if isinstance(v.get(f), list):
            v[f] = [a for a in v[f] if str(a).lower() not in drop]
    return v


def set_item_bands():
    """For a server still on a pre-EoK expansion: the id band of the items its guild has looted this expansion."""
    dated = {c for c, _ in itemdb.LAUNCHES}   # these have item dates
    for g in DATA['guilds']:
        cx = current_xpac(g['server']) or {}
        code = cx.get('code')
        if not code or code in dated:
            itemdb.BANDS.pop(g['server'], None)
            continue
        names = {i[1] for m in g['members'] for i in m.get('cur') or [] if len(i) > 5 and i[5] == code}
        b = itemdb.band_of([itemdb.item_id(n) for n in names])
        if b:
            itemdb.BANDS[g['server']] = (code, b[0], b[1])
        else:
            itemdb.BANDS.pop(g['server'], None)


def tag_inv(inv, server=None):
    """Add each worn item's own expansion (from the item database) to an inventory."""
    if not isinstance(inv, list):
        return inv
    out = []
    for w in inv:
        w = dict(w, xp=itemdb.xpac(w.get('item'), server, w.get('id')))
        augs = w.get('augs') or []
        w['axp'] = [None if a == 'Empty' else itemdb.xpac(a, server) for a in augs]   # each aug's own expansion
        if augs and not w.get('augt'):
            w['augt'] = itemdb.augtypes(w.get('id')) or []
        out.append(w)
    return out




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
                max=AA['cumulative'][code], next=dict(code=nxt[0], xpac=AA['names'][nxt[0]], at=nxt[1]) if nxt else None)


set_item_bands()
for _g in DATA['guilds']:
    for _m in _g['members']:
        if _m.get('inv'):
            _m['inv'] = tag_inv(_m['inv'], _g['server'])


def user(request):
    u = request.session.get('user')
    if not u or u.get('exp', 0) < time.time():
        if request.url.path.startswith('/api/'):     # say so plainly instead of "no access" when a session runs out
            raise HTTPException(401, "You've been signed out (sign-ins last 12 hours). Sign in again and retry.")
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
    if kind == 'raidnotes':                           # "Glask …" lines from raid chat: Leadership and Raid Leaders only
        return not write and (scope in u.get('lead', []) or scope in u.get('rl', []))
    if kind == 'trackach':
        return bool(u['access']) and not write
    if kind == 'slots':
        return bool(u['access']) and not write          # shared item -> slot list; changed through /api/slots
    if kind == 'qcount':
        return any(a == 'officer' for a in u['access'].values())
    level = u['access'].get(scope)
    if not level:
        return False
    if level == 'officer':
        return True
    if level == 'member':
        parts = key.split('-', 3)
        own = len(parts) == 4 and bool(my_main(u, scope)) and parts[3] == my_main(u, scope)
        if kind in ('inv', 'ach'):
            return own
        return (not write) and kind in ('signup', 'hidden')
    if write:
        return kind in RECRUIT_WRITE
    return kind not in OFFICER_ONLY_READ


def links(server):
    return store.get('glask-links-' + server) or {}


def my_main(u, server):
    l = links(server).get(u['id'])
    return l['main'] if l and l.get('status') == 'approved' else None


def find_main(server, char):
    g = next((x for x in DATA['guilds'] if x['server'] == server), None)
    if not g:
        return None, None
    for m in g['members']:
        if any(c['n'].lower() == char.strip().lower() for c in m['chars']):
            return g, m
    return g, None


def anon_raids(raids, own, overrides):
    """Raid parses with everyone but this member's own characters made anonymous (class and numbers only)."""
    out = []
    for r in raids or []:
        r = dict(r); rl = r.get('raid_leader'); ps = []
        for p in r.get('players', []):
            p = dict(p); o = overrides.get(p.get('name', '').lower())
            if o:
                p['cls'] = o; p['played'] = o
            if p.get('name', '').lower() not in own:
                is_rl = bool(rl) and p.get('main') == rl
                p['name'] = ''; p['main'] = '__rl__' if is_rl else ''; p['killed_by'] = []
            ps.append(p)
        r['players'] = ps; r['raid_leader'] = '__rl__' if rl else rl
        out.append(r)
    return out


def own_rows(qs, own):
    return [dict(q, rows=[r for r in q.get('rows', []) if any(str(c).strip().lower() in own for c in r)]) for q in qs or []]


def member_guild(g, main, kv):
    """What a member may see of their guild: their own record, plus a bare name/class roster for reading raid logs."""
    s = g['server']
    me = next((m for m in g['members'] if m['main'] == main), None)
    own = {c['n'].lower() for c in me['chars']} if me else set()
    ovr = kv.get('glask-cls-' + s) or {}
    others = [dict(main=m['main'], cls=m['cls'], rank='', level='', last=None, days=None, loot_n=0, dkp=0, cur=[], cur_main=0,
                   cur_slots=[], slot_last={}, app=None, inv=None, other=[], flags={}, vq_seen={}, raid_ach={}, achx=None,
                   chars=[dict(n=c['n'], c=c['c'], a=c['a']) for c in m['chars']], _other=True)
              for m in g['members'] if m['main'] != main]
    att = {d: [x for x in l if x == main] for d, l in (g.get('attendance') or {}).items()}
    out = dict(g, members=([me] if me else []) + others, departed=[], unreadable=[], apps=[],
               recruits=[r for r in g.get('recruits', []) if r['main'] == main], questionnaires=own_rows(g.get('questionnaires'), own),
               raids=anon_raids(g.get('raids'), own, ovr), attendance={d: l for d, l in att.items() if l})
    mkv = {}
    for k, v in kv.items():
        parts = k.split('-', 3)
        if len(parts) < 3 or parts[2] != s:
            continue
        kind = parts[1]
        if kind in ('inv', 'ach') and len(parts) == 4 and parts[3] == main:
            mkv[k] = v
        elif kind == 'raids':
            mkv[k] = anon_raids(v, own, ovr)
        elif kind == 'att':
            mkv[k] = {d: [x for x in l if x == main] for d, l in v.items() if main in l}
        elif kind == 'q':
            mkv[k] = own_rows(v, own)
        elif kind == 'base':
            mkv[k] = {c: b for c, b in v.items() if c in own}
        elif kind in ('signup', 'hidden'):
            mkv[k] = v
        elif kind == 'seen':
            mkv[k] = {c: a for c, a in v.items() if c in own}
        elif kind == 'uploads':
            mkv[k] = [x for x in v if x.get('uid')]   # filtered per user below
    return out, mkv


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
    full = store.all()
    guilds = []; kv = {}; mylinks = {}
    for g in allowed:
        s = g['server']
        l = links(s).get(u['id'])
        mylinks[s] = dict(main=l['main'], status=l['status']) if l else None
        if u['access'][s] == 'member':
            mg, mkv = member_guild(g, l['main'] if l and l['status'] == 'approved' else None, full)
            guilds.append(mg); kv.update(mkv)
        else:
            guilds.append(g)
            kv.update({k: v for k, v in full.items() if k.split('-')[2:3] == [s] and not k.startswith('glask-fights-') and can(u, k, write=False)})
    kv['glask-trackach-all'] = dict(TRACK_DEFAULT, **(full.get('glask-trackach-all') or {}))
    kv.update({k: v for k, v in full.items() if k == 'glask-slots-all' or (k.startswith(('glask-logsettings-', 'glask-qcount-')) and can(u, k, False))})
    data = dict(DATA, guilds=guilds)
    data['loot_status'] = {g['server']: live_loot.STATUS.get(g['server']) for g in allowed}
    data['app_status'] = {g['server']: live_loot.ASTATUS.get(g['server']) for g in allowed if u['access'][g['server']] != 'member'}
    if 'officer' in u['access'].values():
        try:
            data['sa_email'] = json.loads(os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON') or '{}').get('client_email')
        except Exception:
            data['sa_email'] = None
    data['qsheets'] = {g['server']: [dict(s, status=live_loot.QSTATUS.get(s['id'])) for s in live_loot.qsheets(store, g['server'])]
                       for g in allowed if u['access'][g['server']] == 'officer'}
    data['review_from'] = REVIEW_FROM
    data['flagtree'] = FLAGS
    data['ach_ambig'] = ACH_AMBIG
    data['ach_multi'] = ACH_MULTI
    data['aa'] = dict(buffer=AA['buffer'], **{g['server']: current_xpac(g['server']) for g in allowed})
    set_item_bands()
    for k in [k for k in kv if k.startswith('glask-inv-')]:
        kv[k] = tag_inv(kv[k], k.split('-')[2])
    for k in [k for k in kv if k.startswith('glask-uploads-')]:
        kv[k] = uploads_for(u, k.split('-', 2)[2], kv[k])
    me = dict(id=u['id'], name=u['name'], avatar=u.get('avatar'), access=u['access'], lead=u.get('lead', []), rl=u.get('rl', []), links=mylinks)
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
performance improvement plans. Other members of a guild's Discord see only their own character page (their gear, achievements, AA,
their own raid performance compared with anonymous class averages, and their launch sign-up), plus the guild's character names and classes
so that raid logs they upload can be read. Members link their Discord account to their character; links are approved automatically
when the Discord username matches their guild application, or by an officer.</p>

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
            try:
                why = tok.json(); why = f"{why.get('error', '')}: {why.get('error_description', '')}".strip(': ')
            except Exception:
                why = (tok.text or '')[:120].replace('<', ' ')
            print(f'Discord token exchange failed: HTTP {tok.status_code} {why}', flush=True)
            import html as _h
            return page('<p>Discord sign-in failed. Try again.</p>'
                        f'<p style="color:#8a847b;font-size:13px">Discord answered {tok.status_code}: {_h.escape(why or "no reason given")}</p>'
                        '<a class="btn" href="/login">Sign in with Discord</a>')
        h = {'Authorization': 'Bearer ' + tok.json()['access_token']}
        me = (await c.get(DISCORD_API + '/users/@me', headers=h)).json()
        access = {}; lead = []; rl = []
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
            access[server] = 'member'                         # anyone in the guild's Discord gets their own character page
            if roles & cfg['leader']:
                lead.append(server)
            if roles & cfg.get('raidlead', set()):
                rl.append(server)
            if roles & (cfg['officer'] | cfg['leader']):
                access[server] = 'officer'
            elif roles & cfg['recruit']:
                access[server] = 'recruit'
            if (me.get('username') or '').lower() in TEMP_OFFICERS.get(server, set()):
                access[server] = 'officer'                   # temporary access granted by Leadership
    name = me.get('global_name') or me.get('username')
    av = f"https://cdn.discordapp.com/avatars/{me['id']}/{me['avatar']}.png?size=64" if me.get('avatar') else None
    # Roles are re-checked at each sign-in; sessions last 12 hours so role changes take effect the same day.
    request.session['user'] = dict(id=me['id'], name=name, username=me.get('username', ''), avatar=av, access=access, lead=lead, rl=rl, exp=time.time() + 12 * 3600)
    store.audit(f"{name} ({me['id']})", 'sign-in', '', json.dumps(access))
    return RedirectResponse('/')


@app.get('/dev-login')
def dev_login(request: Request, level: str = 'officer'):
    if not DEV_LOGIN:
        raise HTTPException(404)
    request.session['user'] = dict(id='dev-' + level, name='Dev ' + level, avatar=None, exp=time.time() + 3600,
                                   username='dev' + level,
                                   access={s: 'officer' if level in ('leader', 'raidlead') else level for s in ('Mischief', 'Teek')},
                                   lead=['Mischief', 'Teek'] if level == 'leader' else [], rl=['Teek'] if level == 'raidlead' else [])
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
    kind, scope = key_parts(key)
    if kind == 'ach':
        value = strip_ach_export(value, future_ach(scope))
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


VIOLATION = 'Illegal bid (violation)'
LOOT_ACTIONS = ('Warning', 'Item reassigned', 'DKP penalty', 'Other')


@app.post('/api/lootrev')
async def loot_review(request: Request):
    """Officers resolve a flagged loot issue (or reopen it, or add a note to it). Keyed by the exact items, so new loot re-flags it.
    Each resolution keeps a copy of the items, so a member's loot history survives sheet edits. A reopened resolution is
    archived, never erased, so violations count forever."""
    u = user(request)
    b = await request.json()
    server, key = b.get('server', ''), str(b.get('key', ''))[:6000]
    if not u or u['access'].get(server) != 'officer':
        raise HTTPException(403, 'Only officers can resolve loot issues')
    who = f"{u['name']} ({u['id']})"; now = time.time()
    with REC_LOCK:
        k, hk = 'glask-lootrev-' + server, 'glask-loothist-' + server
        cur = store.get(k) or {}
        if b.get('op') == 'note':
            text = str(b.get('text', '')).strip()[:2000]
            if not text or key not in cur:
                raise HTTPException(400, 'Nothing to add a note to')
            cur[key].setdefault('notes', []).append(dict(by=u['name'], at=now, text=text))
        elif b.get('reopen'):
            old = cur.pop(key, None)
            if old:
                hist = store.get(hk) or []
                hist.append(dict(old, key=key, reopened_by=u['name'], reopened_at=now))
                store.put(hk, hist, who)
        else:
            how = str(b.get('how', ''))[:60]; note = str(b.get('note', '')).strip()[:2000]
            action = str(b.get('action', ''))[:40] if how == VIOLATION else ''
            if how == VIOLATION and not note:
                raise HTTPException(400, 'A violation needs a note saying what happened')
            if action and action not in LOOT_ACTIONS:
                raise HTTPException(400, 'Unknown action')
            items = [[str(x)[:120] for x in (i or [])[:3]] for i in (b.get('items') or [])[:40]]
            cur[key] = dict(by=u['name'], at=now, how=how, note=note, action=action, main=str(b.get('main', ''))[:40],
                            kind=str(b.get('kind', ''))[:40], slot=str(b.get('slot', ''))[:40], items=items,
                            notes=[dict(by=u['name'], at=now, text=note)] if note else [])
        store.put(k, cur, who)
    return {'rev': cur, 'hist': store.get(hk) or []}


def _norm_handle(h):
    return re.sub(r'#\d+$', '', (h or '').strip().lstrip('@')).lower()


@app.post('/api/link')
async def link_character(request: Request):
    """A member says which character is theirs. Auto-approved when their Discord username matches that character's application."""
    u = user(request)
    b = await request.json()
    server, char = b.get('server', ''), str(b.get('char', ''))[:40]
    if not u or server not in u['access']:
        raise HTTPException(403, "You're not in that guild's Discord")
    g, m = find_main(server, char)
    if not m:
        raise HTTPException(404, f"Glask can't find a character named {char} in the {server} guild roster")
    apps = [a for a in g.get('apps', []) if (a.get('member') or '').lower() == m['main'].lower()]
    auto = any(_norm_handle(a.get('discord')) and _norm_handle(a.get('discord')) == _norm_handle(u.get('username')) for a in apps)
    with REC_LOCK:
        k = 'glask-links-' + server
        cur = store.get(k) or {}
        taken = [x for i, x in cur.items() if i != u['id'] and x.get('main') == m['main'] and x.get('status') == 'approved']
        status = 'approved' if auto and not taken else 'pending'
        cur[u['id']] = dict(main=m['main'], asked=char, status=status, name=u['name'], username=u.get('username', ''),
                            at=time.time(), by='auto (application Discord matches)' if status == 'approved' else None)
        store.put(k, cur, f"{u['name']} ({u['id']})")
    return dict(main=m['main'], status=status)


@app.post('/api/link-decide')
async def link_decide(request: Request):
    u = user(request)
    b = await request.json()
    server, uid, ok = b.get('server', ''), str(b.get('uid', '')), bool(b.get('approve'))
    if not u or u['access'].get(server) != 'officer':
        raise HTTPException(403, 'Only officers approve character links')
    with REC_LOCK:
        k = 'glask-links-' + server
        cur = store.get(k) or {}
        if uid not in cur:
            raise HTTPException(404, 'No such request')
        if ok:
            cur[uid].update(status='approved', by=u['name'], decided=time.time())
        else:
            cur.pop(uid)
        store.put(k, cur, f"{u['name']} ({u['id']})")
    return cur


def _rebuild_att(raids):
    att = {}
    for r in raids:   # attendance from uploads is rebuilt from the counted raid logs
        att[r['date']] = sorted({p['main'] for p in r['players'] if p.get('main')} | set(att.get(r['date'], [])))
    return att


def _totals(raid):
    return {p['name']: [int(p.get('dmg') or 0), int(p.get('heal') or 0)] for p in raid.get('players', []) if p.get('name')}


def raid_checks(server):
    """Compare every log submitted for the same night. Honest logs can show LESS for someone (range, chat filters)
    but never MORE, so a log showing a player well above another log of the same night is flagged."""
    src = dict(store.get('glask-raidsrc-' + server) or {})
    g = next((x for x in DATA['guilds'] if x['server'] == server), {})
    for r in g.get('raids') or []:
        src.setdefault(f"{r['id']}|{r['date']}", [])
        src[f"{r['id']}|{r['date']}"] = [dict(by='starting data', name='Starting data', level='officer', totals=_totals(r))] + \
            [x for x in src[f"{r['id']}|{r['date']}"] if x.get('by') != 'starting data']
    out = {}
    ok = store.get('glask-raidchk-' + server) or {}
    for night, subs in src.items():
        if len(subs) < 2:
            continue
        if ok.get(night, {}).get('sig') == _sig(subs):
            continue                                     # an officer marked these logs reviewed (a new log for the night brings it back)
        issues = []
        for i, a in enumerate(subs):
            for b in subs:
                if a is b:
                    continue
                for who, (d1, h1) in a['totals'].items():
                    if who not in b['totals']:
                        continue
                    d2, h2 = b['totals'][who]
                    for kind, x, y in (('damage', d1, d2), ('healing', h1, h2)):
                        if x > y * 1.15 and x - y > 5_000_000:   # 15% and 5M more than another log saw
                            issues.append(dict(log=a['name'], level=a['level'], player=who, kind=kind, this=x, other=y, other_log=b['name']))
        if issues:
            issues.sort(key=lambda z: -(z['this'] - z['other']))
            out[night] = dict(logs=[dict(name=s_['name'], level=s_['level'], at=s_.get('at'), by=s_['by'], counted=_counted(server, night, s_)) for s_ in subs], issues=issues[:40])
    return out


def _sig(subs):
    return '|'.join(sorted(f"{s_['by']}:{int(s_.get('at') or 0)}" for s_ in subs))


def _counted(server, night, s_):
    if s_['by'] == 'starting data':
        return True
    rid, date = night.split('|', 1)
    r = next((x for x in (store.get('glask-raids-' + server) or []) if x.get('id') == rid and x.get('date') == date), None)
    return bool(r and r.get('uploaded_by') == s_['by'])


def _names(r):
    return {(p.get('name') or '').lower() for p in r.get('players', []) if p.get('name')}


def _split_id(server, raid, *lists):
    """Logs from the same night that share most of their players are the same raid. Logs that share few players are
    separate splits, so each gets its own id (e.g. 'Raid night · split 2') instead of replacing the other."""
    base = raid['id'].split(' · split ')[0]
    g = next((x for x in DATA['guilds'] if x['server'] == server), {})
    hid = set(store.get('glask-hidden-' + server) or [])
    same_day = [r for lst in (*lists, [r for r in g.get('raids') or [] if f"{r['id']}|{r['date']}" not in hid]) for r in lst
                if r.get('date') == raid['date']]
    mine = _names(raid)
    best, score = None, 0.0
    for r in same_day:
        other = _names(r)
        ov = len(mine & other) / max(1, min(len(mine), len(other)))
        if ov > score:
            best, score = r, ov
    if best is not None and score >= 0.5:
        return best['id']                                # same raid as a log we already have
    if not same_day:
        return base
    ids = {r['id'] for r in same_day}
    n = 2
    while f'{base} · split {n}' in ids:
        n += 1
    return f'{base} · split {n}'


@app.post('/api/raid-add')
async def raid_add(request: Request):
    """Officer logs count straight away. A member's (or recruitment team's) log only counts once an officer approves it,
    and if an officer log already covers that night it is kept just for cross-checking."""
    u = user(request)
    b = await request.json()
    server, raid = b.get('server', ''), b.get('raid') or {}
    lvl = (u or {}).get('access', {}).get(server)
    if not u or not lvl:
        raise HTTPException(403, "You don't have access to that guild")
    if lvl == 'member' and not my_main(u, server):
        raise HTTPException(403, 'Link your character first')
    if not raid.get('id') or not raid.get('date') or not isinstance(raid.get('players'), list):
        raise HTTPException(400, "That doesn't look like a parsed raid")
    if len(json.dumps(raid)) > 3_000_000:
        raise HTTPException(413, 'Too large')
    who = f"{u['name']} ({u['id']})"
    trusted = lvl == 'officer'
    raid['uploaded_by'] = u['id']
    raid['uploaded_name'] = (my_main(u, server) or u['name']) if not trusted else u['name']
    fights = raid.pop('fights', None)                  # per-fight detail is stored on its own key and fetched on demand
    notes = raid.pop('glask_notes', None) or []
    new_raid = raid
    with REC_LOCK:
        rk, ak, pk, sk = ('glask-raids-' + server, 'glask-att-' + server, 'glask-raidpend-' + server, 'glask-raidsrc-' + server)
        raids, pend, src = store.get(rk) or [], store.get(pk) or [], store.get(sk) or {}
        sent = len(raid['players'])
        raid['id'] = _split_id(server, raid, raids, pend)
        night = f"{raid['id']}|{raid['date']}"
        subs = [x for x in src.get(night, []) if x.get('by') != u['id']]
        subs.append(dict(by=u['id'], name=raid['uploaded_name'], level='officer' if trusted else 'member', at=time.time(), totals=_totals(raid)))
        src[night] = subs
        store.put(sk, src, who)
        g = next((x for x in DATA['guilds'] if x['server'] == server), {})
        covered = any(r.get('id') == raid['id'] and r.get('date') == raid['date'] for r in raids) or \
            any(r.get('id') == raid['id'] and r.get('date') == raid['date'] for r in (g.get('raids') or []))
        if trusted and any(r['id'] == raid['id'] and r['date'] == raid['date'] for r in g.get('raids') or []):
            hk = 'glask-hidden-' + server             # the same night came with Glask's starting data: the upload replaces it
            hid = store.get(hk) or []
            if night not in hid:
                hid.append(night); store.put(hk, hid, who)
        if trusted:
            same = next((r for r in raids if r.get('id') == raid['id'] and r.get('date') == raid['date']), None)
            if same and len(same.get('players', [])) > len(raid['players']):
                raid = same                               # the same night was already counted from a fuller officer log: keep that one
                status = 'kept'
            raids = [r for r in raids if not (r.get('id') == raid['id'] and r.get('date') == raid['date'])]
            raid['verified'] = 'officer log'
            raids.append(raid)
            pend = [r for r in pend if not (r.get('id') == raid['id'] and r.get('date') == raid['date'])]   # an officer log covers it now
            store.put(rk, raids, who); store.put(ak, _rebuild_att(raids), who); store.put(pk, pend, who)
            status = 'kept' if raid.get('uploaded_by') != u['id'] else 'counted'
        elif covered:
            status = 'checked'                             # an officer log already covers this night: used for cross-checking only
        else:
            same = next((r for r in pend if r.get('id') == raid['id'] and r.get('date') == raid['date']), None)
            subm = sorted(set((same or {}).get('submitted_by', [])) | {raid['uploaded_name']})
            if same and len(same.get('players', [])) >= len(raid['players']):
                raid = same
            raid['submitted_by'] = subm
            pend = [r for r in pend if not (r.get('id') == raid['id'] and r.get('date') == raid['date'])] + [raid]
            store.put(pk, pend, who)
            status = 'pending'
        if notes:                                     # chat lines that mention Glask, kept per raid night, never duplicated
            nk = 'glask-raidnotes-' + server
            allnotes = store.get(nk) or {}
            day = allnotes.setdefault(raid['date'], [])
            seenk = {(n.get('t'), n.get('who'), n.get('text')) for n in day}
            for n in notes[:200]:
                n = dict(t=int(n.get('t') or 0), who=str(n.get('who', ''))[:40], ch=str(n.get('ch', ''))[:20], text=str(n.get('text', ''))[:500])
                if (n['t'], n['who'], n['text']) not in seenk:
                    day.append(n); seenk.add((n['t'], n['who'], n['text']))
            day.sort(key=lambda n: n['t'])
            store.put(nk, allnotes, who)
        if fights is not None:
            for f in fights:
                f['av'] = 2
            raid['upt_v'] = 2
        if fights is not None and raid is new_raid and status in ('counted', 'pending'):
            store.put(f"glask-fights-{server}-{raid['id']}|{raid['date']}", fights, who)
        att = store.get(ak) or {}
    if lvl == 'member':
        main = my_main(u, server)
        m = next((x for x in g['members'] if x['main'] == main), None)
        own = {c['n'].lower() for c in m['chars']} if m else set()
        ovr = store.get('glask-cls-' + server) or {}
        return dict(status=status, saved_as=raid['id'], raids=anon_raids(raids, own, ovr), att={d: [main] for d, l in att.items() if main in l})
    return dict(status=status, saved_as=raid['id'], sent=sent, hidden=store.get('glask-hidden-' + server) or [], kept_n=len(raid['players']), kept_by=raid.get('uploaded_name'),
                raids=raids, att=att, pend=pend if trusted else None)


@app.post('/api/raid-approve')
async def raid_approve(request: Request):
    """Officers: count (or discard) a raid log a member submitted for a night no officer log covers."""
    u = user(request)
    b = await request.json()
    server, rid, date, ok = b.get('server', ''), b.get('id', ''), b.get('date', ''), bool(b.get('ok'))
    if not u or u['access'].get(server) != 'officer':
        raise HTTPException(403, 'Only officers can approve raid logs')
    who = f"{u['name']} ({u['id']})"
    with REC_LOCK:
        rk, ak, pk = 'glask-raids-' + server, 'glask-att-' + server, 'glask-raidpend-' + server
        raids, pend = store.get(rk) or [], store.get(pk) or []
        r = next((x for x in pend if x.get('id') == rid and x.get('date') == date), None)
        if not r:
            raise HTTPException(404, 'That log is no longer waiting for review')
        pend = [x for x in pend if x is not r]
        if ok:
            r['verified'] = 'approved by ' + u['name']
            raids = [x for x in raids if not (x.get('id') == rid and x.get('date') == date)] + [r]
            store.put(rk, raids, who); store.put(ak, _rebuild_att(raids), who)
        store.put(pk, pend, who)
        if not ok:
            store.put(f'glask-fights-{server}-{rid}|{date}', None, who)
        store.audit(who, 'approve raid' if ok else 'reject raid', pk, f'{rid} {date}')
    return dict(raids=store.get(rk) or [], att=store.get(ak) or {}, pend=pend)


@app.post('/api/raid-check-act')
async def raid_check_act(request: Request):
    """Officers: mark a night's disagreement reviewed, or drop one submitted log from the comparison."""
    u = user(request)
    b = await request.json()
    server, night, act = b.get('server', ''), str(b.get('night', '')), b.get('action')
    if not u or u['access'].get(server) != 'officer':
        raise HTTPException(403, 'Officers only')
    who = f"{u['name']} ({u['id']})"
    with REC_LOCK:
        src = store.get('glask-raidsrc-' + server) or {}
        subs = src.get(night) or []
        if act == 'reviewed':
            ok = store.get('glask-raidchk-' + server) or {}
            full = list(subs)
            g = next((x for x in DATA['guilds'] if x['server'] == server), {})
            if any(f"{r['id']}|{r['date']}" == night for r in g.get('raids') or []):
                full = [dict(by='starting data')] + full
            ok[night] = dict(sig=_sig(full), by=u['name'], at=time.time())
            store.put('glask-raidchk-' + server, ok, who)
        elif act == 'remove':
            by = str(b.get('by', ''))
            if _counted(server, night, dict(by=by)):
                raise HTTPException(400, "That's the log being counted. Delete the raid log under Raid logs instead.")
            src[night] = [s_ for s_ in subs if s_['by'] != by]
            store.put('glask-raidsrc-' + server, src, who)
        else:
            raise HTTPException(400, 'Unknown action')
        store.audit(who, 'raid check ' + act, 'glask-raidsrc-' + server, night)
    return raid_checks(server)


@app.get('/api/fights')
async def get_fights(request: Request, server: str = '', id: str = '', date: str = ''):
    """Per-fight detail for one raid night. Officers and the recruitment team get everyone; a member gets only their own
    characters, their own deaths, and the medians of their own class (never anyone else's numbers or names)."""
    u = user(request)
    lvl = (u or {}).get('access', {}).get(server)
    if not u or not lvl:
        raise HTTPException(403, "You don't have access to that guild")
    fights = store.get(f'glask-fights-{server}-{id}|{date}')
    if fights is None:
        return {'fights': None}
    if lvl != 'member':
        return {'fights': fights}
    main = my_main(u, server)
    g = next((x for x in DATA['guilds'] if x['server'] == server), {})
    m = next((x for x in g.get('members', []) if x['main'] == main), None) if main else None
    own = {c['n'] for c in m['chars']} if m else set()
    out = []
    for f in fights:
        P = {n: p for n, p in (f.get('P') or {}).items() if n in own}
        if not P:
            continue
        cls = {p['c'] for p in P.values()}
        out.append(dict(name=f.get('name'), start=f.get('start'), dur=f.get('dur'), P=P,
                        cm={c: v for c, v in (f.get('cm') or {}).items() if c in cls},
                        tkm=f.get('tkm') or {}, hm=f.get('hm') or 0, sw=f.get('sw') or 0,   # group medians and the raid's swing count name no one
                        deaths=[d for d in f.get('deaths', []) if d[1] in own],
                        tt=[x for x in f.get('tt', []) if x[1] in own]))   # tattle tale: only their own failed mechanics
    return {'fights': out}


_PROCS = None


@app.get('/api/procs')
async def get_procs(request: Request):
    """Proc spell -> the ability or buff that triggers it, from EQLogParser's procs.txt (Apache 2.0, see data/procs.NOTICE.txt)."""
    global _PROCS
    u = user(request)
    if not u or not u['access']:
        raise HTTPException(403, 'Sign in first')
    if _PROCS is None:
        _PROCS = json.load(open(Path(__file__).resolve().parent.parent / 'data' / 'procs.json'))
    return _PROCS


_SPELLCLS = None


@app.get('/api/spellclass')
async def get_spellclass(request: Request):
    """Spell or ability name -> the one class that can use it, from the class masks in EQLogParser's spells.txt (Apache 2.0)."""
    global _SPELLCLS
    u = user(request)
    if not u or not u['access']:
        raise HTTPException(403, 'Sign in first')
    if _SPELLCLS is None:
        _SPELLCLS = json.load(open(Path(__file__).resolve().parent.parent / 'data' / 'spellclass.json'))
    return _SPELLCLS


@app.get('/api/raid-checks')
async def raid_checks_ep(request: Request, server: str = ''):
    u = user(request)
    if not u or u['access'].get(server) != 'officer':
        raise HTTPException(403, 'Officers only')
    return raid_checks(server)


@app.post('/api/signup')
async def set_signup(request: Request):
    u = user(request)
    b = await request.json()
    server = b.get('server', '')
    if not u or u['access'].get(server) != 'officer':
        raise HTTPException(403, 'Only officers can set the launch sign-up')
    url = str(b.get('url', '')).strip()[:500]
    if url and not url.startswith('https://'):
        raise HTTPException(400, 'Use a full https:// link')
    v = dict(title=str(b.get('title', '')).strip()[:120], url=url, due=str(b.get('due', ''))[:40], by=u['name'], at=time.time()) if url else None
    store.put('glask-signup-' + server, v, f"{u['name']} ({u['id']})")
    return v or {}


@app.post('/api/item-info')
async def item_info(request: Request):
    u = user(request)
    if not u or not u['access']:
        raise HTTPException(401, 'Sign in again')
    b = await request.json()
    names = [str(n)[:120] for n in (b.get('names') or [])][:200]
    server = str(b.get('server', ''))[:20]
    out = {n: dict(slot=itemdb.slot(n), xp=itemdb.xpac(n, server)) for n in names}
    ids = [str(i)[:12] for i in (b.get('ids') or [])][:60]
    if ids:
        out['_aug'] = {i: itemdb.augtypes(i) for i in ids}
    return out


# Loot review only flags issues that include something looted on/after this date (a guild's fresh start).
# Teek restarted its review on 2026-11-15 after the main/FF round split was added.
REVIEW_FROM = {'Teek': os.environ.get('TEEK_REVIEW_FROM', '2026-11-16'), 'Mischief': os.environ.get('MISCHIEF_REVIEW_FROM', '')}


TRACK_DEFAULT = {
    'UF': ['Underfoot Adept', 'Underfoot Veteran', 'Coin of Brell', 'Emblem of Brell'],
    'TOB': ['Novice The Outer Brood Hunter', 'Adept The Outer Brood Hunter', 'Veteran The Outer Brood Hunter'],
    'HoT': ['Rusty Gate Key', 'Tremulous Key', 'Sanctum Key'],
}


@app.post('/api/trackach')
async def set_tracked(request: Request):
    """Officers choose which achievements Glask tracks for an expansion (one list per expansion, shared by both guilds)."""
    u = user(request)
    if not u or 'officer' not in u['access'].values():
        raise HTTPException(403, 'Only officers can change tracked achievements')
    b = await request.json()
    code = str(b.get('code', ''))[:8]
    names = [str(n).strip()[:120] for n in (b.get('names') or []) if str(n).strip()][:40]
    if code not in AA['names']:
        raise HTTPException(400, 'Unknown expansion')
    with REC_LOCK:
        cur = dict(TRACK_DEFAULT, **(store.get('glask-trackach-all') or {}))
        cur[code] = names
        store.put('glask-trackach-all', cur, f"{u['name']} ({u['id']})")
    return cur


@app.post('/api/ach-seen')
async def ach_seen(request: Request):
    """Merge achievements heard in a log ("Your guildmate X has completed Y achievement.") into the guild's record."""
    u = user(request)
    b = await request.json()
    server, seen = b.get('server', ''), b.get('seen') or {}
    lvl = (u or {}).get('access', {}).get(server)
    if not u or lvl != 'officer':
        raise HTTPException(403, 'Only officer uploads count achievements heard in logs')
    g = next((x for x in DATA['guilds'] if x['server'] == server), None)
    known = {c['n'].lower() for m in g['members'] for c in m['chars']} if g else set()
    future = future_ach(server)
    with REC_LOCK:
        k = 'glask-seen-' + server
        cur = store.get(k) or {}
        n = 0
        for ch, achs in list(seen.items())[:2000]:
            ch = str(ch).lower()[:40]
            if ch not in known or not isinstance(achs, dict):
                continue
            row = cur.setdefault(ch, {})
            for a, d in list(achs.items())[:500]:
                a, d = str(a)[:120], str(d)[:10]
                if a.lower() in future:
                    continue
                if a not in row or d < row[a]:
                    row[a] = d; n += 1
        store.put(k, cur, f"{u['name']} ({u['id']})")
    return cur


@app.post('/api/pip')
async def pip_change(request: Request):
    """One change to a Performance Improvement Plan: update its fields, or add a note (notes are never overwritten)."""
    u = user(request)
    b = await request.json()
    server, main, op = b.get('server', ''), str(b.get('main', ''))[:40], b.get('op')
    if not u or u['access'].get(server) != 'officer':
        raise HTTPException(403, 'Only officers can change PIPs')
    who = u['name']; now = time.time()
    with REC_LOCK:
        k = f'glask-pip-{server}-{main}'
        p = store.get(k) or {}
        log = p.setdefault('log', [])
        if p.get('notes') and not any(n.get('legacy') for n in log):     # free-text notes from before entries existed
            log.insert(0, dict(by='earlier notes', at=p.get('created') or 0, text=p.pop('notes'), legacy=True))
        if op == 'set':
            f = b.get('fields') or {}
            changed = []
            for key in ('status', 'start', 'concern', 'targets', 'review'):
                if key in f and str(f[key])[:2000] != str(p.get(key, '')):
                    p[key] = str(f[key])[:2000]; changed.append(key)
            p.setdefault('created', now); p.setdefault('opened_by', who)
            if changed:
                log.append(dict(by=who, at=now, text='Updated ' + ', '.join(changed) + (f" (status: {p.get('status')})" if 'status' in changed else ''), auto=True))
        elif op == 'note':
            text = str(b.get('text', '')).strip()[:3000]
            if not text:
                raise HTTPException(400, 'Empty note')
            log.append(dict(by=who, at=now, text=text))
        else:
            raise HTTPException(400, 'Unknown change')
        store.put(k, p, f"{who} ({u['id']})")
    return p


@app.post('/api/qsheet')
async def qsheet(request: Request):
    """Officers link (or unlink) a launch sign-up Google Sheet that Glask reads live."""
    u = user(request)
    b = await request.json()
    server = b.get('server', '')
    if not u or u['access'].get(server) != 'officer':
        raise HTTPException(403, 'Only officers can link sign-up sheets')
    m = re.search(r'/d/([A-Za-z0-9_-]{20,})', str(b.get('url', ''))) or re.match(r'^([A-Za-z0-9_-]{20,})$', str(b.get('url', '')).strip())
    with REC_LOCK:
        lst = list(live_loot.qsheets(store, server))
        if b.get('remove'):
            lst = [s for s in lst if s['id'] != b['remove']]
        else:
            if not m:
                raise HTTPException(400, 'Paste the Google Sheet link (docs.google.com/spreadsheets/d/...)')
            sid = m.group(1)
            lst = [s for s in lst if s['id'] != sid] + [dict(id=sid, title=str(b.get('title', '')).strip()[:80] or 'Launch sign-up',
                                                             date=time.strftime('%Y-%m-%d'))]
        store.put('glask-qsheets-' + server, lst, f"{u['name']} ({u['id']})")
    if not b.get('remove'):
        threading.Thread(target=live_loot.refresh_q, args=(DATA, store, [server]), daemon=True).start()
    return [dict(s, status=live_loot.QSTATUS.get(s['id'])) for s in lst]


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
        store.put(f'glask-fights-{server}-{rid}|{date}', None, who)
        src = store.get('glask-raidsrc-' + server) or {}
        if f'{rid}|{date}' in src:
            src.pop(f'{rid}|{date}'); store.put('glask-raidsrc-' + server, src, who)
        if len(keep) != len(raids):
            store.put(rk, keep, who)
            store.put(ak, _rebuild_att(keep), who)
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
            if f not in ('recruited_by', 'contact', 'sme', 'aa_override'):
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


HEALER_CLASSES = {'Cleric', 'Shaman', 'Druid'}


def _med(a):
    a = sorted(a)
    return a[(len(a) - 1) // 2] if a else 0


def _upgrade_fights(fights):
    """Fights saved before 2026-11-17 counted active time as single seconds with a hit. Rebuild it from each player's
    10-second record (gaps of one empty slice count as active), then redo damage/healing per second and the class medians."""
    if not fights or all(f.get('av') == 2 for f in fights):
        return False
    for f in fights:
        if f.get('av') == 2:
            continue
        dur = max(1, f.get('dur') or 1)
        for p in (f.get('P') or {}).values():
            tl = p.get('tl') or []
            on = [i for i, v in enumerate(tl) if v]
            n = 0
            if on:
                s = e = on[0]
                for i in on[1:]:
                    if i - e <= 2:
                        e = i
                    else:
                        n += e - s + 1; s = e = i
                n += e - s + 1
            a = max(p.get('a') or 1, min(dur, n * 10))
            p['a'] = a
            p['v'] = round(((p.get('h') if p.get('c') in HEALER_CLASSES else p.get('d')) or 0) / max(a, 1))
        for c, cm in (f.get('cm') or {}).items():
            ps = [p for p in f['P'].values() if p.get('c') == c and not p.get('b') and p['a'] >= 0.3 * dur]
            if ps:
                cm['v'] = _med([p['v'] for p in ps]); cm['n'] = len(ps)
        f['av'] = 2
    return True


def migrate_active_time():
    """One-time: recount active time in fights already uploaded, and measure each night's % active against fight time."""
    try:
        for k, v in list(store.all().items()):
            if k.startswith('glask-fights-') and isinstance(v, list) and _upgrade_fights(v):
                store.put(k, v, 'glask (active time recount)')
        for g in DATA['guilds']:
            s = g['server']
            for key in ('glask-raids-' + s, 'glask-raidpend-' + s):
                raids = store.get(key) or []
                changed = False
                for r in raids:
                    if r.get('upt_v') == 2:
                        continue
                    fights = store.get(f"glask-fights-{s}-{r['id']}|{r['date']}") or []
                    total = sum(f.get('dur') or 0 for f in fights)
                    if total >= 60:
                        for p in r.get('players', []):
                            p['uptime'] = min(1, (p.get('active') or 0) / total)
                    r['upt_v'] = 2; changed = True
                if changed:
                    store.put(key, raids, 'glask (active time recount)')
    except Exception as e:
        print('active time recount failed:', e)


def seed_hot_launch():
    """Once: read Teek's House of Thule sign-up sheet live and point Teek members' sign-up button at the HoT form."""
    try:
        if store.get('glask-seed-hot-teek'):
            return
        lst = store.get('glask-qsheets-Teek')
        if lst is not None and not any(s.get('id') == '1NXixLaxe1TotL5oEJiY9P03_f4y4Erjvf2KgrrQ0R8k' for s in lst):
            store.put('glask-qsheets-Teek', lst + [dict(id='1NXixLaxe1TotL5oEJiY9P03_f4y4Erjvf2KgrrQ0R8k', title='House of Thule Launch', date='2026-11-21')], 'glask (HoT launch)')
        store.put('glask-signup-Teek', dict(title='House of Thule Launch', url='https://forms.gle/PkSfvsFK8DooPPtSA', due='', by='glask', at=time.time()), 'glask (HoT launch)')
        store.put('glask-seed-hot-teek', True, 'glask (HoT launch)')
    except Exception as e:
        print('HoT launch seed failed:', e)


def clear_test_hot_teek():
    """Once: remove House of Thule achievements from Teek's log-heard record and achievement exports (they came from test-server uploads)."""
    try:
        if store.get('glask-fix-hot-teek'):
            return
        drop, who = FLAG_NAMES.get('HoT', set()), 'glask (clear test-server HoT achievements)'
        seen = store.get('glask-seen-Teek') or {}
        n = 0
        for ch, achs in seen.items():
            for a in [a for a in achs if a.lower() in drop]:
                achs.pop(a); n += 1
        seen = {c: a for c, a in seen.items() if a}
        store.put('glask-seen-Teek', seen, who)
        for k, v in store.all().items():
            if k.startswith('glask-ach-Teek-') and isinstance(v, dict):
                nv = strip_ach_export(v, drop)
                if nv != v:
                    n += sum(len(v.get(f) or []) - len(nv.get(f) or []) for f in ('done', 'raidDone'))
                    store.put(k, nv, who)
        store.audit(who, 'clear', 'glask-seen-Teek', f'{n} House of Thule achievements removed')
        store.put('glask-fix-hot-teek', True, who)
        print('cleared', n, 'Teek HoT achievements')
    except Exception as e:
        print('HoT clean-up failed:', e)


def clear_unverified_twins():
    """Once: drop raid/mission twin achievements that were taken from log lines without checking for a raid fight.
    Vanquisher ones were already checked (they're kept); everything else on ACH_AMBIG comes back from exports or new officer logs."""
    try:
        if store.get('glask-fix-ach-twins'):
            return
        who, amb = 'glask (raid/mission twins from logs)', {a.lower() for a in ACH_AMBIG}
        for g in DATA['guilds']:
            vq = {n.lower() for _, evs in (g.get('vanq') or []) for e in evs for n in [e['name']] + [x['name'] for x in e.get('subs') or []]}
            k = 'glask-seen-' + g['server']
            seen = store.get(k) or {}
            n = 0
            for ch, achs in seen.items():
                for a in [a for a in achs if a.lower() in amb and a.lower() not in vq]:
                    achs.pop(a); n += 1
            if n:
                store.put(k, {c: a for c, a in seen.items() if a}, who)
                store.audit(who, 'clear', k, f'{n} unverified raid/mission twin achievements removed')
            print(g['server'], 'removed', n, 'unverified twin achievements')
        store.put('glask-fix-ach-twins', True, who)
    except Exception as e:
        print('twin clean-up failed:', e)


@app.on_event('startup')
def _start_live_loot():
    seed_hot_launch()
    clear_test_hot_teek()
    clear_unverified_twins()
    live_loot.start(DATA, AA, store)
    threading.Thread(target=migrate_active_time, daemon=True).start()


@app.post('/api/loot-refresh')
def loot_refresh(request: Request):
    u = user(request)
    if not u or 'officer' not in u['access'].values():
        raise HTTPException(403, 'Only officers can refresh')
    servers = [s for s, lvl in u['access'].items() if lvl == 'officer']
    return live_loot.refresh(DATA, AA, servers, store)


@app.get('/api/audit')
def audit(request: Request):
    u = user(request)
    if not u or not u.get('lead'):
        raise HTTPException(403)
    return JSONResponse(store.recent_audit())


@app.get('/healthz')
def health():
    return {'ok': True}
