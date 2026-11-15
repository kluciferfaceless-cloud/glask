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


def xpac(name):
    """Expansion code the item was added in (EoK onward), or None."""
    r = _rec(name)
    if not r or not r[1]:
        return None
    import datetime
    added = datetime.date.fromisoformat(r[1]) + datetime.timedelta(days=21)
    code = None
    for c, d in LAUNCHES:
        if d <= added.isoformat():
            code = c
    return code
