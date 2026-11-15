"""Item slots from raidloot.com raid pages (e.g. /raid/uf1, /raid/uf2, /raid/uf3, /raid/ufarmor).

Each raid page lists every drop as a link to /items/<id> followed by a detail block with "Slot: Ear".
We read the pages for the expansions a guild is in (plus the one before and after), match "Slot:" lines to the
item link just above them, and save item name -> slot. Officer tags and uploaded inventories take priority in the app.
"""
import html, re, threading, time

import httpx

BASE = 'https://www.raidloot.com/raid/'
PAGES = ['1', '2', '3', '4', 'armor']
STATUS = {}
ITEM_A = re.compile(r'<a[^>]+href="[^"]*/items/(\d+)[^"]*"[^>]*>(.*?)</a>', re.S | re.I)
SLOT = re.compile(r'Slot:\s*([A-Za-z ,]+?)\s*(?:<|\n|$)')
CANON = {'ear': 'Ear', 'ears': 'Ear', 'finger': 'Fingers', 'fingers': 'Fingers', 'wrist': 'Wrist', 'wrists': 'Wrist', 'head': 'Head',
         'face': 'Face', 'neck': 'Neck', 'shoulders': 'Shoulders', 'shoulder': 'Shoulders', 'arms': 'Arms', 'back': 'Back',
         'range': 'Range', 'hands': 'Hands', 'primary': 'Primary', 'secondary': 'Secondary', 'chest': 'Chest', 'legs': 'Legs',
         'feet': 'Feet', 'waist': 'Waist', 'charm': 'Charm', 'power source': 'Power Source', 'ammo': 'Ammo'}


def norm(name):
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', '', name))).replace('`', "'").strip().lower()


def parse(page_html):
    """Return {item name: 'Slot' or 'Slot/Slot'} from one raid page."""
    marks = []
    for m in ITEM_A.finditer(page_html):
        name = html.unescape(re.sub(r'<[^>]+>', '', m.group(2))).strip()
        if name:
            marks.append((m.start(), 'item', name))
    for m in SLOT.finditer(page_html):
        marks.append((m.start(), 'slot', m.group(1)))
    marks.sort()
    out = {}; last = None
    for _, kind, val in marks:
        if kind == 'item':
            last = val
        elif last and last not in out:
            slots = [CANON.get(s.strip().lower()) for s in val.split(',')]
            slots = [s for s in slots if s and s != 'Ammo']
            if slots:
                out[last] = '/'.join(dict.fromkeys(slots))
    return out


def fetch_expansion(code, client):
    found = {}; pages = 0
    for p in PAGES:
        r = client.get(BASE + code.lower() + p)
        if r.status_code != 200:
            continue
        got = parse(r.text)
        if got:
            pages += 1; found.update(got)
        time.sleep(1.5)          # be polite
    return found, pages


def refresh(store, aa, servers=('Mischief', 'Teek')):
    """Read raidloot for each guild's previous, current and next expansion and merge into glask-slots-raidloot."""
    import datetime
    today = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
    codes = []
    for s in servers:
        sched = [c for c, at in aa['schedule'][s]]
        past = [c for c, at in aa['schedule'][s] if at <= today]
        if past:
            i = sched.index(past[-1])
            codes += sched[max(0, i - 1):i + 2]
    codes = list(dict.fromkeys(codes))
    cur = store.get('glask-slots-raidloot') or {}
    try:
        with httpx.Client(timeout=30, follow_redirects=True, headers={'User-Agent': 'Glask guild tool (Faceless EverQuest guild)'}) as c:
            for code in codes:
                found, pages = fetch_expansion(code, c)
                cur.update(found)
                STATUS[code] = dict(items=len(found), pages=pages, at=time.time())
        store.put('glask-slots-raidloot', cur, 'raidloot.com')
    except Exception as e:
        STATUS['error'] = str(e)[:200]
    return STATUS


def start(store, aa, every_days=1):
    def loop():
        while True:
            refresh(store, aa)
            time.sleep(every_days * 86400)
    threading.Thread(target=loop, daemon=True).start()
