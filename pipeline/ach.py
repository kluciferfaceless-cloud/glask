"""Achievements: reference flag list from a fully-flagged character's achievement export, plus
guildmate completions harvested from raw logs ("Your guildmate X has completed Y achievement.")."""
import re, json, collections
GM = re.compile(r'^\[\w{3} (\w{3}) (\d+) [\d:]+ (\d{4})\] Your guildmate (\S+) has completed (.+) achievement\.$')
MON = {m: i for i, m in enumerate('Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec'.split(), 1)}

def reference(path, expansions):
    """Per expansion: completed Progression achievements + completed top-level 'Conqueror of ...' raid achievements."""
    out = {x: dict(progression=[], raids=[]) for x in expansions}; cat = None
    for line in open(path, encoding='latin-1'):
        line = line.rstrip('\r\n')
        if not line.startswith(('C\t', 'I\t')):
            cat = line; continue
        if line.startswith(('C\t\t', 'I\t\t')) or not line.startswith('C\t'):
            continue
        name = line[2:]
        for x in expansions:
            if cat == f'{x}: Progression':
                out[x]['progression'].append(name)
            elif cat == f'{x}: Raids' and name.startswith('Conqueror of'):
                out[x]['raids'].append(name)
    return out

def harvest(paths):
    seen = collections.defaultdict(dict)
    for p in paths:
        for line in open(p, encoding='latin-1'):
            if 'Your guildmate' not in line: continue
            m = GM.match(line.rstrip('\r\n'))
            if m:
                mon, d, y, who, ach = m.groups()
                seen[who.lower()].setdefault(ach, f'{y}-{MON[mon]:02d}-{int(d):02d}')
    return seen

LATER = ['Rain of Fear', 'Call of the Forsaken', 'The Darkened Sea', 'The Broken Mirror', 'Empires of Kunark', 'Ring of Scale',
         'The Burning Lands', 'Torment of Velious', 'Claws of Veeshan', 'Terror of Luclin', 'Night of Shadows', "Laurion's Song", 'The Outer Brood']

def raid_conquerors(path, expansions):
    out = {x: [] for x in expansions}; cat = None
    for line in open(path, encoding='latin-1'):
        line = line.rstrip('\r\n')
        if not line.startswith(('C\t', 'I\t')):
            cat = line; continue
        if line.startswith(('C\t\t', 'I\t\t')): continue
        x = cat[:-len(': Raids')] if cat and cat.endswith(': Raids') else None
        if x in out and 'Conqueror' in line:
            out[x].append(line[2:])
    return out

def vanquishers(path, expansions):
    """Per expansion: Vanquisher events with their sub-achievements (+ description from each sub's own entry)."""
    tops = {}; order = []; cat = None; cur = None
    for line in open(path, encoding='latin-1'):
        line = line.rstrip('\r\n')
        if not line.startswith(('C\t', 'I\t')):
            cat = line; cur = None; continue
        x = cat[:-len(': Raids')] if cat and cat.endswith(': Raids') else None
        if x not in expansions: continue
        if line.startswith(('C\t\t', 'I\t\t')):
            if cur: tops[(x, cur)].append(line[3:])
        else:
            cur = line[2:]; tops[(x, cur)] = []; order.append((x, cur))
    out = {}
    for x in expansions:
        evs = []
        for (xx, name) in order:
            if xx != x or not name.startswith('Vanquisher of'): continue
            subs = [dict(name=s_, desc=(tops.get((x, s_)) or [''])[0]) for s_ in tops[(x, name)]]
            evs.append(dict(name=name, event=name.replace('Vanquisher of ', ''), subs=subs))
        # drop roll-ups whose subs are covered by 2+ other events
        def covered(e):
            others = [set(s_['name'] for s_ in o['subs']) for o in evs if o is not e and len(o['subs']) < len(e['subs'])]
            mine = set(s_['name'] for s_ in e['subs'])
            return len(others) >= 2 and len(mine & set().union(*others)) >= 0.9 * len(mine)
        out[x] = [e for e in evs if not covered(e)]
    return out

if __name__ == '__main__':
    ref = reference('data/ach_Klucifer.txt', ['Underfoot', 'The Outer Brood'])
    print(json.dumps(ref, indent=1))
    mis = harvest(['logs/eqlog_Yamm_mischief.txt', 'logs/dock.txt'])
    teek = harvest(['logs/eqlog_Klucifer_teek.txt'])
    vq = vanquishers('data/ach_Klucifer.txt', ['Underfoot'] + LATER)
    later = raid_conquerors('data/ach_Klucifer.txt', LATER)
    json.dump(dict(ref=ref, vanq=vq, later=later, later_order=LATER, mis=mis, teek=teek), open('data/ach.json', 'w'))
    for name, s in (('mis', mis), ('teek', teek)):
        allx = set(ref['The Outer Brood' if name == 'mis' else 'Underfoot']['progression'] + ref['The Outer Brood' if name == 'mis' else 'Underfoot']['raids'])
        print(name, len(s), 'chars;', sum(1 for v in s.values() if allx & set(v)), 'with a reference achievement')
        print(collections.Counter(a for v in s.values() for a in v if a in allx).most_common(15))
