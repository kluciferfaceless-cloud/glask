"""Item facts from the eqitems.com item database dump (data/item_info.json.gz: lowercase name -> [EQ slot bitmask, date added]).

Slots: the bitmask was checked against 42 items from real /outputfile inventory exports and against raidloot.com raid pages.
Expansion: the date an item was added to the game, matched to live launch dates (Allakhazam's expansion list). Items added up to
3 weeks before a launch belong to that launch (they appear during pre-launch). Dates before 2016 are a bulk import, so items
from before Empires of Kunark have no expansion here.
"""
import gzip, json
from pathlib import Path

BITS = [(0, 'Charm'), (1, 'Ear'), (2, 'Head'), (3, 'Face'), (4, 'Ear'), (5, 'Neck'), (6, 'Shoulders'), (7, 'Arms'), (8, 'Back'),
        (9, 'Wrist'), (10, 'Wrist'), (11, 'Range'), (12, 'Hands'), (13, 'Primary'), (14, 'Secondary'), (15, 'Fingers'),
        (16, 'Fingers'), (17, 'Chest'), (18, 'Legs'), (19, 'Feet'), (20, 'Waist'), (21, 'Power Source')]
LAUNCHES = [('EoK', '2016-11-16'), ('RoS', '2017-12-12'), ('TBL', '2018-12-11'), ('ToV', '2019-12-18'), ('CoV', '2020-12-08'),
            ('ToL', '2021-12-07'), ('NoS', '2022-12-06'), ('LS', '2023-12-05'), ('TOB', '2024-12-03'), ('SoR', '2025-12-02')]
_DB = None


def _db():
    global _DB
    if _DB is None:
        _DB = json.load(gzip.open(Path(__file__).resolve().parent.parent / 'data' / 'item_info.json.gz', 'rt'))
    return _DB


def _rec(name):
    return _db().get((name or '').strip().lower().replace('`', "'"))


def slot(name):
    """'Ear', 'Primary/Secondary', 'Not gear' (can't be worn), or None (not in the database)."""
    r = _rec(name)
    if r is None:
        return None
    names = list(dict.fromkeys(n for b, n in BITS if r[0] & (1 << b)))
    return '/'.join(names) if names else 'Not gear'


_IDS = None
BANDS = {}           # server -> (expansion code, lowest id, highest id) for a server still on a pre-EoK expansion


def item_id(name):
    """The item's id in the eqitems dump (data/item_ids.json.gz), or None."""
    global _IDS
    if _IDS is None:
        _IDS = json.load(gzip.open(Path(__file__).resolve().parent.parent / 'data' / 'item_ids.json.gz', 'rt'))
    return _IDS.get((name or '').strip().lower().replace('`', "'"))


def band_of(ids, min_n=20, gap=300):
    """The densest run of item ids (no two neighbours more than `gap` apart): an expansion's items are numbered together."""
    xs = sorted(set(i for i in ids if i))
    best, run = None, []
    for x in xs:
        if run and x - run[-1] > gap:
            if not best or len(run) > len(best):
                best = run
            run = []
        run.append(x)
    if run and (not best or len(run) > len(best)):
        best = run
    return (best[0], best[-1]) if best and len(best) >= min_n else None


def xpac(name, server=None, iid=None):
    """Expansion code the item was added in, or None.
    EoK onward comes from the date the item was added. Items from before EoK have no date here, so for a server still on an
    older expansion, an item numbered inside that expansion's id band (from the guild's loot this expansion) gets its code."""
    r = _rec(name)
    if (not r or not r[1]) and server in BANDS:
        code, lo, hi = BANDS[server]
        i = iid if iid else item_id(name)
        try:
            i = int(i)
        except (TypeError, ValueError):
            i = None
        return code if i and lo <= i <= hi else None
    if not r or not r[1]:
        return None
    import datetime
    added = datetime.date.fromisoformat(r[1]) + datetime.timedelta(days=21)
    code = None
    for c, d in LAUNCHES:
        if d <= added.isoformat():
            code = c
    return code


_AUG = None


def augtypes(item_id):
    """The six aug slot types of an item (eqitems dump augslot1type..augslot6type), or None if unknown / no aug slots."""
    global _AUG
    if _AUG is None:
        _AUG = json.load(gzip.open(Path(__file__).resolve().parent.parent / 'data' / 'aug_types.json.gz', 'rt'))
    return _AUG.get(str(item_id or '').strip())


CLASS_BITS = ['Warrior', 'Cleric', 'Paladin', 'Ranger', 'Shadow Knight', 'Druid', 'Monk', 'Bard', 'Rogue', 'Shaman',
              'Necromancer', 'Wizard', 'Magician', 'Enchanter', 'Beastlord', 'Berserker']   # EQ class bitmask order (checked against the loot sheet)
ALL_CLASSES = (1 << 16) - 1


def classes(name):
    """Class bitmask of an item, or 0 when the item doesn't say (unknown, every class, or no class restriction such as tradeskill tokens)."""
    r = _rec(name)
    if not r or len(r) < 3 or not r[2] or (r[2] & ALL_CLASSES) == ALL_CLASSES:
        return 0
    return r[2] & ALL_CLASSES
