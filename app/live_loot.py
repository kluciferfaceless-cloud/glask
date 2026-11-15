"""Read the guild loot sheets live from Google Sheets and refresh each member's loot in the dataset.

Needs a Google service account (GOOGLE_SERVICE_ACCOUNT_JSON) that each loot sheet is shared with as a Viewer.
"""
import collections, datetime, io, json, os, re, threading, time

import httpx

from pipeline.parse_loot import parse as parse_workbook
from . import itemdb

SHEETS = {
    'Mischief': os.environ.get('MISCHIEF_LOOT_SHEET', '1pir-iBjwI9fCCb2lNd4X0RZyTelqI4bgYHck6wfK_CQ'),   # TF DKP Sheet 3.0
    'Teek': os.environ.get('TEEK_LOOT_SHEET', '16MrS3TCywaJ-o2JkaB15vyImIWClstcT_hW30naK0A8'),           # Faceless Fury DKP Sheets
}
XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
TOKEN_RE = re.compile(r'(?i)^Obscured (?:[A-Za-z\' ]+? )?(Head|Arms|Wrist|Hands|Chest|Legs|Feet) Armor')
STATUS = {}          # server -> dict(ok, at, msg, entries, nights)
QSTATUS = {}         # sheet id -> dict(ok, at, msg, rows)
# launch sign-up sheets read live (officers add more on Launch planning); first tab = form responses
QSHEETS_DEFAULT = {'Teek': [dict(id='17gFCNHKS4nWrKoEsFKG0AfzyxALYNVifWKTwnB_hG6g', title='Underfoot Launch', date='2026-09-15')]}


def qsheets(store, server):
    lst = store.get('glask-qsheets-' + server) if store else None
    return lst if lst is not None else QSHEETS_DEFAULT.get(server, [])


def fetch_q(sheet, token):
    import csv
    r = httpx.get(f"https://www.googleapis.com/drive/v3/files/{sheet['id']}/export", params={'mimeType': 'text/csv'},
                  headers={'Authorization': 'Bearer ' + token}, timeout=60)
    if r.status_code in (403, 404):
        raise RuntimeError("not shared with Glask's service account (share it as Viewer)")
    r.raise_for_status()
    rows = [x for x in csv.reader(io.StringIO(r.content.decode('utf-8-sig'))) if any(c.strip() for c in x)]
    if not rows:
        raise RuntimeError('the first tab is empty')
    hdr = rows[0]
    data = [x + [''] * (len(hdr) - len(x)) for x in rows[1:]]
    return dict(id=sheet.get('title') or sheet['id'], title=sheet.get('title') or 'Launch sign-up', date=sheet.get('date', ''),
                header=hdr, rows=data, sheet=sheet['id'], live=True)


APPSHEETS = {'Mischief': os.environ.get('MISCHIEF_APP_SHEET', '14tCqL1MuJAm9Q-d_aQ3TSOQn0QSjaNpDvJ12X9X3otY'),   # Faceless Mischief Application
             'Teek': os.environ.get('TEEK_APP_SHEET', '1yBHGooneh0qRU5Xm8q0ScXlYkQaZkncBjMO9QmTvSgs')}         # Faceless 25th Application
ASTATUS = {}
# application form questions -> Glask field, matched by words in the column header (so reordering questions is fine)
APP_COLS = [('name', r'character name|char.*name|^name'), ('cls', r'class'), ('level', r'level'), ('aa', r'\baa\b|alternate adv'),
            ('boxes', r'box'), ('discord', r'discord'), ('prior', r'prior|previous|other guild'), ('racing', r'racing'),
            ('latest_xpac', r'latest expansion|completed on a prior'), ('notes', r'^notes|anything else|comment')]


def _pd(s):
    m = re.match(r'(\d{1,2})/(\d{1,2})/(\d{4})', s or '')
    return datetime.date(int(m.group(3)), int(m.group(1)), int(m.group(2))) if m else datetime.date(1900, 1, 1)


def refresh_apps(data, servers=None, token=None):
    import csv
    try:
        token = token or _token()
    except Exception:
        return ASTATUS
    for server in servers or APPSHEETS:
        g = next((x for x in data['guilds'] if x['server'] == server), None)
        sid = APPSHEETS.get(server)
        if not g or not sid:
            continue
        try:
            r = httpx.get(f'https://www.googleapis.com/drive/v3/files/{sid}/export', params={'mimeType': 'text/csv'},
                          headers={'Authorization': 'Bearer ' + token}, timeout=60)
            if r.status_code in (403, 404):
                raise RuntimeError("not shared with Glask's service account (share it as Viewer)")
            r.raise_for_status()
            rows = [x for x in csv.reader(io.StringIO(r.content.decode('utf-8-sig'))) if any(c.strip() for c in x)]
            hdr = [h.strip().lower() for h in rows[0]]
            col = {}
            for key, rx in APP_COLS:
                i = next((i for i, h in enumerate(hdr) if i and i not in col.values() and re.search(rx, h)), None)
                if i is not None:
                    col[key] = i
            if 'name' not in col:
                raise RuntimeError("couldn't find the character name column")
            apps = []
            for x in rows[1:]:
                x = x + [''] * (len(hdr) - len(x))
                if not x[col['name']].strip():
                    continue
                a = dict(date=x[0].strip(), **{k: x[i].strip() for k, i in col.items()})
                apps.append(a)
            apps.sort(key=lambda a: _pd(a['date']), reverse=True)
            by = {c['n'].lower(): m for m in g['members'] for c in m['chars']}
            for a in apps:
                m = by.get(a['name'].lower())
                a['in_guild'] = bool(m); a['member'] = m['main'] if m else None
            with _lock:
                g['apps'] = apps[:60]
                for m in g['members']:                       # each member's own latest application
                    mine = [a for a in apps if a['member'] == m['main']]
                    if mine:
                        m['app'] = mine[0]
            ASTATUS[server] = dict(ok=True, at=time.time(), msg=f'{len(apps)} applications', newest=apps[0]['date'] if apps else None)
        except Exception as e:
            ASTATUS[server] = dict(ok=False, at=time.time(), msg=str(e)[:200])
    return ASTATUS


def refresh_q(data, store, servers=None, token=None):
    try:
        token = token or _token()
    except Exception as e:
        return QSTATUS
    for server in servers or SHEETS:
        g = next((x for x in data['guilds'] if x['server'] == server), None)
        if not g:
            continue
        for sh in qsheets(store, server):
            try:
                q = fetch_q(sh, token)
                with _lock:
                    g['questionnaires'] = [q] + [x for x in g.get('questionnaires', []) if x.get('sheet') != sh['id'] and x.get('id') != q['id']]
                    g['questionnaires'].sort(key=lambda x: x.get('date', ''), reverse=True)
                QSTATUS[sh['id']] = dict(ok=True, at=time.time(), msg=f"{len(q['rows'])} responses", rows=len(q['rows']))
            except Exception as e:
                QSTATUS[sh['id']] = dict(ok=False, at=time.time(), msg=str(e)[:200])
    return QSTATUS
_lock = threading.Lock()


def _token():
    raw = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON', '')
    if not raw:
        raise RuntimeError('GOOGLE_SERVICE_ACCOUNT_JSON is not set')
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request
    creds = service_account.Credentials.from_service_account_info(
        json.loads(raw), scopes=['https://www.googleapis.com/auth/drive.readonly'])
    creds.refresh(Request())
    return creds.token


def fetch(sheet_id, token):
    r = httpx.get(f'https://www.googleapis.com/drive/v3/files/{sheet_id}/export',
                  params={'mimeType': XLSX}, headers={'Authorization': 'Bearer ' + token}, timeout=120)
    if r.status_code == 404:
        raise RuntimeError("sheet not found, or not shared with the Glask service account")
    if r.status_code == 403:
        raise RuntimeError("Glask's service account doesn't have access to the sheet (share it as Viewer)")
    r.raise_for_status()
    rows, used, _ = parse_workbook(io.BytesIO(r.content))
    return rows, len(used)


def rollup(server, guild, rows, aa, slotmap):
    """Recompute every member's loot fields from fresh sheet rows (same rules as pipeline/build.py)."""
    sched = aa['schedule'][server]
    codes = [c for c, _ in sched]

    def xpac_at(day):
        c = None
        for code, at in sched:
            if at[:10] <= day[:10]:
                c = code
        return c

    now = xpac_at(datetime.datetime.utcnow().strftime('%Y-%m-%d'))
    i = codes.index(now) if now in codes else 0
    keep = set(codes[max(0, i - 1):i + 2])

    def tab_code(t):
        for p, n in (('tob', 'TOB'), ('sor', 'SoR'), ('fof', 'FoF'), ('ls', 'LS'), ('tol', 'ToL'), ('nos', 'NoS'), ('cotf', 'CotF'), ('tds', 'TDS')):
            if t.startswith(p):
                return n
        return ''

    seen = set(); loot = []; order = {}
    for x in rows:
        t = x['tab'].lower().replace('copy of ', '').strip()
        order.setdefault(t, len(order))
        k = (t, x['time'], x['item'].lower(), x['to'].lower())
        if k in seen:
            continue
        seen.add(k); x['_o'] = order[t]; loot.append(x)
    loot.sort(key=lambda x: (x['_o'], [-ord(ch) for ch in x['time']]))
    tabx = {}; last = ''
    for t, _ in sorted(order.items(), key=lambda kv: kv[1]):
        c = tab_code(t) if server == 'Mischief' else ''
        if c:
            last = c
        tabx[t] = c or last

    def code_of(x):
        d = x['time'][:10]
        if server == 'Mischief':
            return xpac_at(d) if re.match(r'\d{4}-\d\d-\d\d', d) else tabx.get(x['tab'].lower().replace('copy of ', '').strip(), '')
        return xpac_at(d) if re.match(r'\d{4}-\d\d-\d\d', d) else ''

    def when(x):
        if re.match(r'\d{4}-\d\d-\d\d', x['time'][:10]):
            return x['time'][:10]
        m = re.match(r'^(.*?)\s*(\d{2,4})$', x['tab'].strip())
        if m:
            d = m.group(2); md = d[0] + '/' + d[1:] if len(d) in (2, 3) else d[:2] + '/' + d[2:]
            return (m.group(1) + ' ' + md).strip()
        return x['tab']

    def slot_of(item):
        m = TOKEN_RE.match(item)
        return m.group(1).capitalize() if m else (slotmap.get(item) or itemdb.slot(item))

    by = collections.defaultdict(list)
    for x in loot:
        by[x['to'].lower()].append(x)
    for mem in guild['members']:
        n_all = 0; dkp = 0; cur = []; slot_last = {}
        for c in mem['chars']:
            for x in by.get(c['n'].lower(), []):
                if 'alt' in x['tab'].lower():
                    continue                      # alt-round tabs never count toward gear or loot
                n_all += 1; dkp += x['cost'] or 0
                xc = code_of(x); sl = slot_of(x['item'])
                if xc in keep:
                    cur.append([when(x), x['item'], c['n'], x['cost'], sl, xc, 1 if 'alt' in x['tab'].lower() else 0])
                if c['n'] == mem['main'] and sl and sl != 'Not gear':
                    for s1 in sl.split('/'):
                        w = when(x); old = slot_last.get(s1)
                        if not old or (re.match(r'\d{4}-', w) and (not re.match(r'\d{4}-', old[0]) or w > old[0])):
                            slot_last[s1] = [w, x['item'], xc]   # newest date looted wins
        mine = [i for i in cur if i[2] == mem['main']]
        mem.update(loot_n=n_all, dkp=dkp, cur=cur, cur_main=len(mine),
                   cur_slots=sorted({s for i in mine if i[4] and i[4] != 'Not gear' for s in i[4].split('/')}), slot_last=slot_last)

    def night(t):
        try:
            return (datetime.datetime.fromisoformat(t[:19]) - datetime.timedelta(hours=6)).date().isoformat()
        except Exception:
            return None
    guild['raid_dates'] = sorted({n for x in loot if 'alt' not in x['tab'].lower() and (n := night(x['time'])) and n >= '2026-01-01'})
    for r in guild.get('recruits', []):
        if r.get('start') and r.get('raids') is not None:
            r['raids'] = sum(1 for d in guild['raid_dates'] if d >= r['start'])
    guild['stats'].update(loot_entries=len(loot), loot_nights=len(order))
    return len(loot), len(order)


def refresh(data, aa, servers=None, store=None):
    """Fetch and apply every loot sheet (and launch sign-up sheet). Returns STATUS."""
    try:
        tok = _token()
    except Exception as e:
        for s in servers or SHEETS:
            STATUS[s] = dict(ok=False, at=time.time(), msg=str(e))
        return STATUS
    for server in servers or SHEETS:
        g = next((x for x in data['guilds'] if x['server'] == server), None)
        if not g or not SHEETS.get(server):
            continue
        try:
            rows, tabs = fetch(SHEETS[server], tok)
            with _lock:
                n, nights = rollup(server, g, rows, aa, data.get('slotmap', {}))
            STATUS[server] = dict(ok=True, at=time.time(), msg=f'{n} loot entries from {nights} tabs', entries=n)
        except Exception as e:
            STATUS[server] = dict(ok=False, at=time.time(), msg=str(e)[:200])
    refresh_q(data, store, servers, tok)
    refresh_apps(data, servers, tok)
    return STATUS


# Mon, Tue, Thu, Fri at 10:00 Eastern (the mornings after raid nights), plus once when Glask starts up
SCHEDULE_DAYS = {0, 1, 3, 4}
SCHEDULE_HOUR = 10


def next_run(now=None):
    from zoneinfo import ZoneInfo
    tz = ZoneInfo('America/New_York')
    now = now or datetime.datetime.now(tz)
    d = now.replace(hour=SCHEDULE_HOUR, minute=0, second=0, microsecond=0)
    while d <= now or d.weekday() not in SCHEDULE_DAYS:
        d = (d + datetime.timedelta(days=1)).replace(hour=SCHEDULE_HOUR)
    return d


def start(data, aa, store=None):
    if not os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON'):
        for s in SHEETS:
            STATUS[s] = dict(ok=False, at=None, msg='Live loot is off: GOOGLE_SERVICE_ACCOUNT_JSON is not set. Showing the Nov 14 snapshot.')
        return

    def loop():
        refresh(data, aa, store=store)         # on startup, so a redeploy never falls back to the snapshot
        while True:
            from zoneinfo import ZoneInfo
            wait = (next_run() - datetime.datetime.now(ZoneInfo('America/New_York'))).total_seconds()
            time.sleep(max(60, wait))
            refresh(data, aa, store=store)
    threading.Thread(target=loop, daemon=True).start()

    def apps_loop():                           # applications change between raids: check them every hour
        while True:
            time.sleep(3600)
            refresh_apps(data)
    threading.Thread(target=apps_loop, daemon=True).start()
