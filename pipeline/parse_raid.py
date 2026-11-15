"""Parse a raw EverQuest raid log into per-player performance numbers (Glask performance module)."""
import re, sys, json, collections, datetime

TS = re.compile(r'^\[\w{3} (\w{3}) (\d+) (\d\d):(\d\d):(\d\d) (\d{4})\] (.*)$')
MELEE = re.compile(r"^(.+?) (?:hit|hits|slash|slashes|crush|crushes|pierce|pierces|kick|kicks|bash|bashes|punch|punches|strike|strikes|bite|bites|backstab|backstabs|shoot|shoots|claw|claws|frenzy on|frenzies on|sting|stings|slam|slams|smash|smashes|maul|mauls|gore|gores) (.+?) for (\d+) points? of (?:(\w+) )?damage(?: by (.+?))?\.(?: \((.+)\))?$")
DOT = re.compile(r"^(.+?) has taken (\d+) damage from (?:your )?(.+?)(?: by (.+?))?\.(?: \((.+)\))?$")
HEAL = re.compile(r"^(.+?) healed (.+?) for (\d+)(?: \((\d+)\))? hit points?(?: by (.+?))?\.(?: \((.+)\))?$")
SLAIN = re.compile(r"^(.+?) (?:has been slain by|have been slain by) (.+?)!$")
OWNER = re.compile(r"^(.+?)`s (?:pet|warder|ward)$")
MON = {m: i for i, m in enumerate('Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec'.split(), 1)}


RANK = re.compile(r'(?: Rk\. ?[IVX]+| [IVXL]+| Effect)+$')
def norm(sp):
    return RANK.sub('', sp.strip())


def owner_of(actor, me):
    if actor in ('You', 'you', 'Your', 'your'):
        return me
    m = OWNER.match(actor)
    if m:
        return me if m.group(1) in ('Your', 'your') else m.group(1)
    return actor


def parse(path, roster, me):
    """roster: dict lowercase name -> (class, main). me: the log owner's character name."""
    is_player = lambda n: n.lower() in roster or n == me
    dmg = collections.Counter(); hits = collections.defaultdict(list); heal = collections.Counter(); over = collections.Counter()
    spells = collections.defaultdict(collections.Counter); deaths = collections.defaultdict(list); acts = collections.defaultdict(list); first = last = None
    for line in open(path, encoding='latin-1'):
        m = TS.match(line.rstrip('\r\n'))
        if not m:
            continue
        mon, d, hh, mm, ss, yy, msg = m.groups()
        t = datetime.datetime(int(yy), MON[mon], int(d), int(hh), int(mm), int(ss)).timestamp()
        x = MELEE.match(msg)
        if x:
            src = owner_of(x.group(1), me); tgt = x.group(2)
            if is_player(src) and not is_player(tgt) and not OWNER.match(tgt):
                n = int(x.group(3)); dmg[src] += n; acts[src].append(t)
                if x.group(5) and not OWNER.match(x.group(1)): spells[src][norm(x.group(5))] += 1
                first = t if first is None else first; last = t
            continue
        x = DOT.match(msg)
        if x:
            tgt = x.group(1); src = owner_of(x.group(4) or 'You', me)
            if 'your ' in msg.split(' damage from ')[1][:5]:
                src = me
            if is_player(src) and not is_player(tgt) and not OWNER.match(tgt):
                n = int(x.group(2)); dmg[src] += n; acts[src].append(t)
                if not OWNER.match(x.group(4) or ''): spells[src][norm(x.group(3))] += 1
                first = t if first is None else first; last = t
            continue
        x = HEAL.match(msg)
        if x:
            src = owner_of(x.group(1), me)
            if is_player(src):
                actual = int(x.group(3)); total = int(x.group(4) or x.group(3))
                heal[src] += actual; over[src] += total - actual; acts[src].append(t)
                if x.group(5) and not OWNER.match(x.group(1)): spells[src][norm(x.group(5))] += 1
            continue
        x = SLAIN.match(msg)
        if x:
            who = me if x.group(1) == 'You' else x.group(1)
            if is_player(who):
                deaths[who].append((t, x.group(2)))
    fight = max(1, (last or 0) - (first or 0))
    # infer the class each player actually played, from spells cast (EQ personas let a character switch class)
    # Single pass, conservative: a spell "belongs" to a class only if >=3 casters and >=60% of them are that class
    # (by guild-dump class); a player is flagged as playing another class only if >=80% of their casts point there.
    votes = collections.defaultdict(collections.Counter)
    for p, sp in spells.items():
        c = roster.get(p.lower(), ('?',))[0]
        if c != '?':
            for name in sp: votes[name][c] += 1
    sclass = {}
    for name, v in votes.items():
        c, n = v.most_common(1)[0]
        if n >= 3 and n / sum(v.values()) >= 0.6:
            sclass[name] = c
    played = {}
    for p, sp in spells.items():
        tally = collections.Counter()
        for name, k in sp.items():
            if name in sclass: tally[sclass[name]] += k
        if tally:
            c, n = tally.most_common(1)[0]
            if n / sum(tally.values()) >= 0.8 and n >= 200:
                played[p] = c
    out = []
    for p in set(dmg) | set(heal):
        ts = sorted(acts[p])
        # active time: merge activity into intervals, bridging gaps under 12s
        active = 0; s = e = None
        for u in ts:
            if s is None:
                s = e = u
            elif u - e <= 12:
                e = u
            else:
                active += e - s + 1; s = e = u
        if s is not None:
            active += e - s + 1
        dump_cls, main = roster.get(p.lower(), ('?', p))
        cls = played.get(p, dump_cls)
        out.append(dict(name=p, main=main, cls=cls, dump_cls=dump_cls, persona=cls != dump_cls and dump_cls != '?', dmg=dmg[p], dps=round(dmg[p] / fight), sdps=round(dmg[p] / max(active, 1)),
                        heal=heal[p], overheal=over[p], active=round(active), uptime=round(min(1, active / fight), 3),
                        deaths=len(deaths[p]), killed_by=[k for _, k in deaths[p]]))
    return dict(start=first, end=last, seconds=round(fight), players=out)


if __name__ == '__main__':
    roster = json.load(open(sys.argv[2]))
    r = parse(sys.argv[1], roster, sys.argv[3])
    json.dump(r, open(sys.argv[4], 'w'))
    print(r['seconds'], len(r['players']))
