"""Glask data layer: parse guild dumps, link alts, join loot + applications, build cleanup queues."""
import json, re, csv, datetime, collections

TODAY = datetime.date(2026, 11, 14)
INACTIVE_DAYS = 90
LINK = re.compile(r'(?i)^\s*(?:alt|raid\s*box|box)\s*[-:]?\s*([A-Za-z`\']+)')


def pdate(s):
    try:
        m, d, y = s.strip().split('/')
        y = int(y); y = y + 2000 if y < 100 else y
        return datetime.date(y, int(m), int(d))
    except Exception:
        return None


# ---- gear helpers ----
# Current expansion comes from Daybreak's unlock calendar (data/aa.json in the Glask repo).
# Each loot entry is tagged with its expansion code; the app keeps only the code that is current right now.
_AA = json.load(open('/home/claude/glask-repo/data/aa.json'))
def xpac_at(server, day):
    c = None
    for code, at in _AA['schedule'][server]:
        if at[:10] <= day[:10]: c = code
    return c
_now = {s: xpac_at(s, TODAY.isoformat()) for s in ('Mischief', 'Teek')}
_codes = {s: [c for c, _ in _AA['schedule'][s]] for s in _AA['schedule']}
_keep = {s: set(_codes[s][_codes[s].index(_now[s]):][:2]) | {_now[s]} for s in _now}   # current + the next one
_keep = {s: _keep[s] | {_codes[s][max(0, _codes[s].index(_now[s]) - 1)]} for s in _now}  # + previous, for context
XPAC_NAME = {s: _AA['names'][_now[s]] for s in _now}
def _xm(x):
    t = x['tab'].lower().replace('copy of ', '')
    for p, n in (('tob', 'TOB'), ('sor', 'SoR'), ('fof', 'FoF'), ('ls', 'LS'), ('tol', 'ToL'), ('nos', 'NoS'), ('cotf', 'CotF'), ('tds', 'TDS')):
        if t.startswith(p): return n
    return x['time'][:7]
XPAC = {'Mischief': _xm, 'Teek': lambda x: xpac_at('Teek', x['time']) if x['time'][:10] else x['time'][:7]}
def code_of(server, x, tabx):
    if server == 'Mischief':
        return tabx.get(x['tab'].lower().replace('copy of ', '').strip(), '') or (xpac_at(server, x['time']) if x['time'][:10] else '')
    return XPAC[server](x)
SLOT_WORDS = {'head': 'Head', 'arms': 'Arms', 'wrist': 'Wrist', 'hands': 'Hands', 'chest': 'Chest', 'legs': 'Legs', 'feet': 'Feet'}
TOKEN = re.compile(r'(?i)^Obscured (?:[A-Za-z\' ]+? )?(Head|Arms|Wrist|Hands|Chest|Legs|Feet) Armor')
SLOTMAP = {}   # item name -> slot, learned from uploaded inventories (worn items only)
INV = {}
WORN = ['Charm','Ear','Head','Face','Neck','Shoulders','Arms','Back','Wrist','Range','Hands','Primary','Secondary','Fingers','Chest','Legs','Feet','Waist','Power Source','Ammo']
def parse_inventory(path):
    out = []
    for line in open(path, encoding='latin-1'):
        r = line.rstrip('\r\n').split('\t')
        if len(r) < 5 or r[0] == 'Location': continue
        base = r[0].split('-Slot')[0]
        if base not in WORN: break
        if '-Slot' in r[0]:
            if out: out[-1]['augs'].append(r[1])
        else:
            out.append(dict(slot=base, item=r[1], id=r[2], augs=[]))
    return out
def learn(path, who):
    inv = parse_inventory(path); INV[who] = inv
    for w in inv:
        if w['item'] != 'Empty':
            SLOTMAP.setdefault(w['item'], w['slot'])
def slot_of(item):
    m = TOKEN.match(item)
    if m: return m.group(1).capitalize()
    return SLOTMAP.get(item)


def load_dump(path):
    chars = []
    for line in open(path, encoding='latin-1'):
        if not line.strip():
            continue
        r = line.rstrip('\r\n').split('\t')
        chars.append(dict(name=r[0], level=int(r[1]) if r[1].isdigit() else r[1], cls=r[2], rank=r[3],
                          alt=r[4] == 'A', last=r[5], zone=r[6], note=r[7], tribute=r[11]))
    return chars


def load_apps_mis(path):
    v = json.load(open(path)); out = {}
    for r in v[1:]:
        r = r + [''] * (8 - len(r))
        if not r[1].strip():
            continue
        out.setdefault(r[1].strip().lower(), []).append(dict(
            date=r[0], name=r[1].strip(), cls=r[2], level=r[3], aa=r[4], boxes=r[5], discord=r[6], prior=r[7]))
    return out


def load_apps_teek(path):
    rows = list(csv.reader(open(path, encoding='utf-8'))); out = {}
    for r in rows[1:]:
        r = r + [''] * (10 - len(r))
        if not r[1].strip():
            continue
        out.setdefault(r[1].strip().lower(), []).append(dict(
            date=r[0], name=r[1].strip(), cls=r[2], racing=r[3], discord=r[4], prior=r[5], notes=r[6],
            level=r[7], latest_xpac=r[8], aa=r[9]))
    return out


def build(guild, server, dump, loot_path, apps):
    chars = load_dump(dump)
    by = {c['name'].lower(): c for c in chars}
    mains = {c['name'].lower(): c for c in chars if not c['alt']}
    members = {k: dict(main=v['name'], chars=[v['name']]) for k, v in mains.items()}
    departed = collections.defaultdict(list)
    unreadable = []
    for c in chars:
        if not c['alt']:
            continue
        m = LINK.match(c['note'])
        if not m:
            unreadable.append((c['name'], c['name'])); c['link'] = None; continue
        t = m.group(1).lower(); seen = set()
        # follow alt -> alt chains until we reach a main, a departed name, or a dead end
        while t in by and by[t]['alt'] and t not in seen:
            seen.add(t)
            m2 = LINK.match(by[t]['note'])
            if not m2:
                break
            t = m2.group(1).lower()
        if t in mains:
            c['link'] = mains[t]['name']; members[t]['chars'].append(c['name'])
        elif t in by:
            c['link'] = None; unreadable.append((c['name'], by[t]['name']))
        else:
            c['link'] = None; departed[t.capitalize()].append(c['name'])

    # loot (dedupe copies of tabs; newest first by timestamp)
    raw = json.load(open(loot_path)); seen = set(); loot = []; order = {}
    for x in raw:
        t = x['tab'].lower().replace('copy of ', '').strip()
        order.setdefault(t, len(order))
        k = (t, x['time'], x['item'].lower(), x['to'].lower())
        if k in seen:
            continue
        seen.add(k); x['_o'] = order[t]; loot.append(x)
    loot.sort(key=lambda x: (x['_o'], [-ord(ch) for ch in x['time']]))
    # expansion per tab: tabs without an expansion prefix (e.g. "ALT 911") inherit from the next newer tab
    tabx = {}; lastx = ''
    for t, _ in sorted(order.items(), key=lambda kv: kv[1]):
        xp = XPAC[server](dict(tab=t, time=''))
        if xp and not re.match(r'^\d{4}-\d\d$', xp) and xp != '':
            lastx = xp
        tabx[t] = xp if xp and not re.match(r'^\d{4}-\d\d$', xp) else lastx
    def when(x):
        if x['time'][:10]: return x['time'][:10]
        m = re.match(r'^(.*?)\s*(\d{2,4})$', x['tab'].strip())
        if m:
            d = m.group(2); md = d[0] + '/' + d[1:] if len(d) in (2, 3) else d[:2] + '/' + d[2:]
            return (m.group(1) + ' ' + md).strip()
        return x['tab']
    loot_by = collections.defaultdict(list)
    for x in loot:
        loot_by[x['to'].lower()].append(x)

    # per member rollups
    out_members = []
    for k, mem in members.items():
        cs = [by[n.lower()] for n in mem['chars']]
        dates = [pdate(c['last']) for c in cs if pdate(c['last'])]
        last = max(dates) if dates else None
        n_all = 0; cur = []; slot_last = {}; dkp = 0
        for c in cs:
            for x in loot_by.get(c['name'].lower(), []):
                n_all += 1; dkp += x['cost'] or 0
                xc = code_of(server, x, tabx)
                is_cur = xc in _keep[server]
                sl = slot_of(x['item'])
                if is_cur:
                    cur.append([when(x), x['item'], c['name'], x['cost'], sl, xc])
                if c['name'] == mem['main'] and sl:
                    for s1 in sl.split('/'):
                        if s1 not in slot_last:
                            slot_last[s1] = [when(x), x['item'], code_of(server, x, tabx)]
        app = None
        for c in cs:
            if c['name'].lower() in apps:
                app = apps[c['name'].lower()][-1]; break
        main_cur = [i for i in cur if i[2] == mem['main']]
        out_members.append(dict(
            main=mem['main'], rank=mains[k]['rank'], cls=mains[k]['cls'], level=mains[k]['level'],
            last=last.isoformat() if last else None,
            days=(TODAY - last).days if last else None,
            chars=[dict(n=c['name'], l=c['level'], c=c['cls'], r=c['rank'], a=c['alt'], s=c['last'], z=c['zone'], note=c['note']) for c in cs],
            loot_n=n_all, dkp=dkp, cur=cur, cur_main=len(main_cur),
            cur_slots=sorted({s1 for i in main_cur if i[4] for s1 in i[4].split('/')}),
            slot_last=slot_last, app=app, inv=INV.get(mem['main'])))
    out_members.sort(key=lambda m: m['main'].lower())

    inactive = [m for m in out_members if m['days'] is None or m['days'] > INACTIVE_DAYS]
    dep = []
    for main, alts in departed.items():
        lasts = [pdate(by[a.lower()]['last']) for a in alts]
        lasts = [d for d in lasts if d]
        dep.append(dict(main=main, alts=[dict(n=a, s=by[a.lower()]['last'], c=by[a.lower()]['cls'], l=by[a.lower()]['level']) for a in alts],
                        last=max(lasts).isoformat() if lasts else None))
    dep.sort(key=lambda d: -len(d['alts']))
    groups = collections.OrderedDict()
    for n, anchor in unreadable:
        groups.setdefault(anchor, [])
        if n != anchor:
            groups[anchor].append(n)
    def cinfo(n):
        c = by[n.lower()]; return dict(n=c['name'], note=c['note'], c=c['cls'], l=c['level'], s=c['last'])
    unread = [dict(anchor=cinfo(a), alts=[cinfo(x) for x in xs]) for a, xs in groups.items()]
    unread.sort(key=lambda u: -len(u['alts']))

    ranks = collections.Counter(m['rank'] for m in out_members)
    stats = dict(chars=len(chars), mains=len(out_members), alts=sum(1 for c in chars if c['alt']),
                 linked=sum(1 for c in chars if c['alt'] and c.get('link')),
                 departed_mains=len(dep), departed_alts=sum(len(d['alts']) for d in dep), unreadable=len(unreadable), unreadable_groups=len(unread),
                 inactive=len(inactive), inactive_raiders=sum(1 for m in inactive if m['rank'] == 'Raider'),
                 raiders=ranks.get('Raider', 0), ranks=ranks.most_common(),
                 loot_entries=len(loot), loot_nights=len({x['tab'] for x in loot}), xpac=XPAC_NAME[server])
    recent_apps = sorted(((a, k) for k, lst in apps.items() for a in lst), key=lambda t: t[0]['date'], reverse=False)
    def ad(a):
        d = a['date'].split(' ')[0]; p = pdate(d); return p or datetime.date(1900, 1, 1)
    all_apps = sorted([a for lst in apps.values() for a in lst], key=ad, reverse=True)[:60]
    for a in all_apps:
        a['in_guild'] = a['name'].lower() in by
        a['member'] = None
        if a['in_guild']:
            c = by[a['name'].lower()]
            a['member'] = c['name'] if not c['alt'] else c.get('link')
    return dict(guild=guild, server=server, dump_time='2026-11-14 15:4x', stats=stats, members=out_members,
                departed=dep, unreadable=unread, apps=all_apps)


if __name__ == '__main__':
    learn('data/inv_Dsuna.txt', 'Dsuna')
    learn('data/inv_Klucifer_old.txt', '_klu_old'); INV.pop('_klu_old')
    mis = build('The Faceless', 'Mischief', 'data/mis_dump.txt', 'data/mis_loot.json', load_apps_mis('data/mis_apps.json'))
    teek = build('Faceless Fury', 'Teek', 'data/teek_dump.txt', 'data/teek_loot.json', load_apps_teek('data/teek_apps.csv'))
    # cross-guild: departed mains that are actually in the other chapter
    for g, other in ((mis, teek), (teek, mis)):
        names = {c['n'].lower(): (m['main'], c['n']) for m in other['members'] for c in m['chars']}
        for d in g['departed']:
            d['elsewhere'] = names.get(d['main'].lower())
        for m in g['members']:
            m['other'] = sorted({names[c['n'].lower()][0] for c in m['chars'] if c['n'].lower() in names})
    # achievements / flagging reference
    A = json.load(open('data/ach.json'))
    for g, key, xp in ((mis, 'mis', 'The Outer Brood'), (teek, 'teek', 'Underfoot')):
        ref = A['ref'][xp]; refset = set(ref['progression'] + ref['raids'])
        g['flag_ref'] = dict(xpac=xp, source='Klucifer (Mischief) achievement export', progression=ref['progression'], raids=ref['raids'])
        seen = A[key]
        for m in g['members']:
            got = {}
            for c in m['chars']:
                for a, d in seen.get(c['n'].lower(), {}).items():
                    if a in refset and (a not in got or d < got[a]): got[a] = d
            m['flags'] = got
        g['ach_chars_seen'] = len(seen)
        # later-era (RoF+) raid achievements, only relevant once a guild has reached Rain of Fear
        order = A['later_order']; idx = order.index(xp) if xp in order else -1
        g['raid_ref'] = [[x, A['later'][x]] for x in order[:idx + 1]] if idx >= 0 else []
        vx = ([xp] if xp not in order else []) + (order[:idx + 1] if idx >= 0 else [])
        g['vanq'] = [[x, A['vanq'][x]] for x in vx]
        allsub = {s_['name'] for _, evs in g['vanq'] for e in evs for s_ in e['subs']} | {e['name'] for _, evs in g['vanq'] for e in evs}
        for m in g['members']:
            got = {}
            for c in m['chars']:
                for a, d in seen.get(c['n'].lower(), {}).items():
                    if a in allsub and (a not in got or d < got[a]): got[a] = d
            m['vq_seen'] = got
        allr = {a for _, lst in g['raid_ref'] for a in lst}
        for m in g['members']:
            got = {}
            for c in m['chars']:
                for a, d in seen.get(c['n'].lower(), {}).items():
                    if a in allr and (a not in got or d < got[a]): got[a] = d
            m['raid_ach'] = got
        # raid nights (for the 6-raid applicant window): distinct dates of main raid tabs (alt rounds excluded)
        lj = json.load(open('data/mis_loot.json' if key == 'mis' else 'data/teek_loot.json'))
        def night(t):  # raid-night date: loot after midnight belongs to the previous evening
            try: return (datetime.datetime.fromisoformat(t[:19]) - datetime.timedelta(hours=6)).date().isoformat()
            except Exception: return None
        g['raid_dates'] = sorted({n for x in lj if 'alt' not in x['tab'].lower() and (n := night(x['time'])) and n >= '2026-01-01'})
        # recruits not yet re-ranked: Applicant/Recruit rank, or a 'Recruit' note on a main
        rec = []
        for m in g['members']:
            mc = next(c for c in m['chars'] if c['n'] == m['main'])
            if re.search(r'(?i)applicant|recruit|trial', m['rank']) or re.match(r'(?i)\s*recruit', mc['note'] or ''):
                start = None
                mm = re.search(r'(\d{1,2})/(\d{1,2})/(\d{2,4})', mc['note'] or '') or re.search(r'(\d{1,2})/(\d{1,2})/(\d{2,4})', (m['app'] or {}).get('date', ''))
                if mm:
                    y = int(mm.group(3)); y = y + 2000 if y < 100 else y
                    start = f'{y}-{int(mm.group(1)):02d}-{int(mm.group(2)):02d}'
                raids = sum(1 for d in g['raid_dates'] if d >= start) if start else None
                if start and g['raid_dates'] and start < g['raid_dates'][0] and raids is not None and raids < 6:
                    raids = None   # started before the loot data we can see; can't count reliably
                rec.append(dict(main=m['main'], rank=m['rank'], cls=m['cls'], level=m['level'], note=mc['note'], days=m['days'],
                                app=(m['app'] or {}).get('date', '').split(' ')[0], start=start, raids=raids,
                                partial=bool(start and g['raid_dates'] and start < g['raid_dates'][0])))
        g['recruits'] = rec
    # achievement exports (/outputfile achievement): AA bracket + definitive flags
    def parse_ach_export(path, wanted):
        done = set(); tiers = []; cat = None
        for line in open(path, encoding='latin-1'):
            line = line.rstrip('\r\n')
            if not line.startswith(('C\t', 'I\t')): cat = line; continue
            if line.startswith(('C\t\t', 'I\t\t')): continue
            name = line[2:]; ok = line[0] == 'C'
            mm = re.match(r'^([\d,]+) Alternate Advancement Points$', name)
            if mm: tiers.append((int(mm.group(1).replace(',', '')), ok))
            if ok and name in wanted: done.add(name)
        got = [t for t, ok in tiers if ok]; nxt = [t for t, ok in tiers if not ok]
        return dict(aa_floor=max(got) if got else 0, aa_next=min(nxt) if nxt else None, done=sorted(done), source='export')
    for g in (mis, teek):
        wanted = set(g['flag_ref']['progression'] + g['flag_ref']['raids']) | {a for _, l in g['raid_ref'] for a in l} \
                 | {s_['name'] for _, evs in g['vanq'] for e in evs for s_ in e['subs']} | {e['name'] for _, evs in g['vanq'] for e in evs}
        if g is mis:
            wanted |= {a for _, l in teek['raid_ref'] for a in l} | {s_['name'] for _, evs in teek['vanq'] for e in evs for s_ in e['subs']}
        g['_wanted'] = sorted(wanted)
    kl = parse_ach_export('data/ach_Klucifer.txt', set(mis['_wanted']))
    for m in mis['members']:
        m['achx'] = kl if m['main'] == 'Klucifer' else None
    for m in teek['members']:
        m['achx'] = None
    # class history (personas): every class a character has shown in any dump we hold, plus raid logs
    import glob
    BASE = {'Mischief': {'klucifer': 'Paladin'}, 'Teek': {}}
    for g, pats in ((mis, ['data/mis_dump*.txt']), (teek, ['data/teek_dump*.txt'])):
        hist = collections.defaultdict(set)
        for pat in pats:
            for f in glob.glob(pat):
                for c in load_dump(f): hist[c['name'].lower()].add(c['cls'])
        if g is mis:
            try:
                for p in json.load(open('logs/dock.json'))['players']:
                    hist[p['name'].lower()].add(p['cls'])
            except Exception: pass
        for m in g['members']:
            for c in m['chars']:
                c['h'] = sorted(hist.get(c['n'].lower(), {c['c']}) | {c['c']})
                b0 = BASE[g['server']].get(c['n'].lower())
                if b0: c['base'] = b0
    # expansion-planning questionnaires (Google Form responses), newest first
    def qload(path, title, date):
        rows = list(csv.reader(open(path, encoding='utf-8')))
        return dict(id=title, title=title, date=date, header=rows[0], rows=[r for r in rows[1:] if any(c.strip() for c in r)])
    mis['questionnaires'] = [qload('data/q_tob.csv', 'TOB Sign Ups', '2026-10-18')]
    teek['questionnaires'] = [qload('data/q_tss.csv', 'TSS Launch (Teek)', '2025-11-16'), qload('data/q_por.csv', 'Prophecy of Ro Launch 10/09', '2025-09-21')]
    data = dict(built=datetime.datetime.utcnow().isoformat() + 'Z', generated=TODAY.isoformat(), inactive_days=INACTIVE_DAYS, guilds=[mis, teek], slotmap=SLOTMAP)
    json.dump(data, open('data/glask.json', 'w'), separators=(',', ':'))
    for g in data['guilds']:
        print(g['server'], {k: v for k, v in g['stats'].items() if k != 'ranks'})
