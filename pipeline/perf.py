import json, statistics
HEAL_CLASSES = {'Cleric', 'Shaman'}
def rank_raid(raid, alts, label, date, rl_main='Klucifer', rl_class='Bard'):
    ps = raid['players']
    for p in ps:
        p['rl'] = False
        if p['name'] == 'Log owner':  # the log owner ("You")
            p.update(name=f"{rl_main} (bard, log owner)", cls=rl_class, main=rl_main, rl=True)
        elif p['main'] == rl_main:
            p['rl'] = True   # raid leader's other characters in the same raid
    for p in ps:
        p['box'] = p['name'] in alts
        p['metric'] = 'hps' if p['cls'] in HEAL_CLASSES else 'sdps'
        p['val'] = round(p['heal'] / max(p['active'], 1)) if p['metric'] == 'hps' else p['sdps']
        # healing far above damage on a non-healer class: role doesn't match the guild-dump class
        p['mismatch'] = p['metric'] == 'sdps' and p['heal'] > 2 * p['dmg'] and p['heal'] > 20_000_000
    by = {}
    for p in ps:
        by.setdefault(p['cls'], []).append(p)
    for cls, group in by.items():
        base = [p['val'] for p in group if not p['box'] and not p['mismatch'] and not p['rl'] and p['uptime'] >= 0.3] or [p['val'] for p in group]
        med = statistics.median(base) if base else 0
        order = sorted(group, key=lambda p: -p['val'])
        for p in group:
            p['pct'] = round(100 * p['val'] / med) if med else None
            p['rank'] = order.index(p) + 1; p['of'] = len(group); p['median'] = med
    return dict(id=label, date=date, seconds=raid['seconds'], raid_leader=rl_main, players=ps)
if __name__ == '__main__':
    d = json.load(open('data/glask.json'))
    alts = {c['n'] for m in d['guilds'][0]['members'] for c in m['chars'] if c['a']} | set(json.load(open('data/mis_alts_hist.json')))
    r = rank_raid(json.load(open('logs/dock.json')), alts, 'Dock raid', '2026-11-12')
    d['guilds'][0]['raids'] = [r]; d['guilds'][1]['raids'] = []
    names = {c['n'].lower(): m['main'] for m in d['guilds'][0]['members'] for c in m['chars']}
    att = sorted({names[p['name'].lower()] for p in r['players'] if p['name'].lower() in names} | {r['raid_leader']})
    d['guilds'][0]['attendance'] = {r['date']: att}; d['guilds'][1]['attendance'] = {}
    json.dump(d, open('data/glask.json', 'w'), separators=(',', ':'))
    for p in sorted(r['players'], key=lambda p: (p['cls'], -p['val'])):
        print(f"{p['cls'][:9]:9} {p['name']:12} {'box' if p['box'] else '   '} {p['metric']} {p['val']/1e3:6.0f}K {p['pct']}% #{p['rank']}/{p['of']} up{p['uptime']:.2f} d{p['deaths']}")
