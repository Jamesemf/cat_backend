"""Sighting coordinates are coarsened before they can be stored or published.

The privacy policy promises that published sighting locations are approximate.
These tests are what makes that sentence true rather than aspirational, so they
assert the guarantee at the schema boundary — the choke point every write path
goes through — not in the router, which a future write path could bypass.

The determinism assertions matter as much as the rounding ones: coarsening only
resists an averaging attack (many sightings of the same cat on the same
doorstep) if repeat sightings inside one cell collapse onto the identical
stored value. Random jitter would satisfy the rounding assertion and fail the
determinism one.
"""

import pytest

from app.schemas.sighting import MatchCheckRequest, SightingCommit
from app.utils.geo import LOCATION_DECIMALS, coarsen

# A precise fix, of the kind a phone actually reports, and where it lands.
PRECISE_LAT, PRECISE_LNG = 51.5074321, -0.1278459
COARSE_LAT, COARSE_LNG = 51.507, -0.128


def commit(lat: float, lng: float) -> SightingCommit:
    return SightingCommit(photo_path="uploads/x.jpg", latitude=lat, longitude=lng)


# --- The helper -------------------------------------------------------------

def test_coarsen_snaps_to_the_grid():
    assert coarsen(PRECISE_LAT) == COARSE_LAT
    assert coarsen(PRECISE_LNG) == COARSE_LNG


def test_coarsen_is_idempotent():
    """The startup backfill re-runs on every boot; it must not drift."""
    once = coarsen(PRECISE_LAT)
    assert coarsen(once) == once


def test_grid_is_about_a_hundred_metres():
    """Guard the privacy promise against someone "just" adding a decimal.

    3 dp is ~111 m of latitude. At 4 dp it is ~11 m, which is a house, and the
    policy's "approximate" would stop being true.
    """
    assert LOCATION_DECIMALS == 3


# --- The schema boundary ----------------------------------------------------

def test_commit_coarsens_on_parse():
    body = commit(PRECISE_LAT, PRECISE_LNG)
    assert (body.latitude, body.longitude) == (COARSE_LAT, COARSE_LNG)


def test_match_check_coarsens_on_parse():
    """Match on the same grid the sighting will be stored on."""
    body = MatchCheckRequest(latitude=PRECISE_LAT, longitude=PRECISE_LNG)
    assert (body.latitude, body.longitude) == (COARSE_LAT, COARSE_LNG)


def test_coarsening_survives_coordinate_bounds_validation():
    """Bounds checking still applies — coarsening must not swallow bad input."""
    with pytest.raises(ValueError):
        commit(999.0, 0.0)


# --- Determinism, i.e. resistance to averaging ------------------------------

def test_neighbouring_fixes_collapse_to_one_point():
    """Two fixes metres apart inside a cell must store as the same coordinate.

    This is the property that stops N sightings of a cat at home from averaging
    out to its front door.
    """
    a = commit(51.507401, -0.127801)
    b = commit(51.507449, -0.127849)
    assert (a.latitude, a.longitude) == (b.latitude, b.longitude)


def test_same_input_always_gives_same_output():
    first = commit(PRECISE_LAT, PRECISE_LNG)
    second = commit(PRECISE_LAT, PRECISE_LNG)
    assert (first.latitude, first.longitude) == (second.latitude, second.longitude)


def test_distant_points_stay_distinct():
    """Coarsening must not be so aggressive the map stops working."""
    here = commit(51.5074, -0.1278)      # Westminster
    there = commit(51.5155, -0.1410)     # Oxford Circus, ~1.2 km away
    assert (here.latitude, here.longitude) != (there.latitude, there.longitude)
