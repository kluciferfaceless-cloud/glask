"""Item -> worn slot, from the eqitems.com item database dump (data/item_slots.json.gz: lowercase name -> EQ slot bitmask).

The bitmask was checked against 42 items seen in real /outputfile inventory exports and against raidloot.com raid pages.
"""
import gzip, json
from pathlib import Path

BITS = [(0, 'Charm'), (1, 'Ear'), (2, 'Head'), (3, 'Face'), (4, 'Ear'), (5, 'Neck'), (6, 'Shoulders'), (7, 'Arms'), (8, 'Back'),
        (9, 'Wrist'), (10, 'Wrist'), (11, 'Range'), (12, 'Hands'), (13, 'Primary'), (14, 'Secondary'), (15, 'Fingers'),
        (16, 'Fingers'), (17, 'Chest'), (18, 'Legs'), (19, 'Feet'), (20, 'Waist'), (21, 'Power Source')]
_DB = None


def _db():
    global _DB
    if _DB is None:
        _DB = json.load(gzip.open(Path(__file__).resolve().parent.parent / 'data' / 'item_slots.json.gz', 'rt'))
    return _DB


def slot(name):
    """'Ear', 'Primary/Secondary', 'Not gear' (can't be worn), or None (not in the database)."""
    m = _db().get((name or '').strip().lower().replace('`', "'"))
    if m is None:
        return None
    if isinstance(m, list):
        v = 0
        for x in m: v |= x
        m = v
    names = list(dict.fromkeys(n for b, n in BITS if m & (1 << b)))
    return '/'.join(names) if names else 'Not gear'
