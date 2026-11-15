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
            return tabx.get(x['tab'].lower().replace('copy of ', '').strip(), '') or (xpac_at(d) if re.match(r'\d{4}-\d\d-\d\d', d) else '')
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
                        slot_last.setdefault(s1, [when(x), x['item'], xc])
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


def refresh(data, aa, servers=None):
    """Fetch and apply every loot sheet. Returns STATUS."""
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


def start(data, aa):
    if not os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON'):
        for s in SHEETS:
            STATUS[s] = dict(ok=False, at=None, msg='Live loot is off: GOOGLE_SERVICE_ACCOUNT_JSON is not set. Showing the Nov 14 snapshot.')
        return

    def loop():
        refresh(data, aa)                      # on startup, so a redeploy never falls back to the snapshot
        while True:
            from zoneinfo import ZoneInfo
            wait = (next_run() - datetime.datetime.now(ZoneInfo('America/New_York'))).total_seconds()
            time.sleep(max(60, wait))
            refresh(data, aa)
    threading.Thread(target=loop, daemon=True).start()
