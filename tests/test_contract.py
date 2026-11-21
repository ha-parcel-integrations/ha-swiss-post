"""The parcel contract's checks, run over every source's normalised samples."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.swiss_post.account.parcels import normalize_account_parcel
from custom_components.swiss_post.const import ParcelStatus
from custom_components.swiss_post.parcels import (
    is_pickup,
    planned_for_status,
    planned_window,
)
from custom_components.swiss_post.tracking.parcels import normalize_parcel

from .payloads import (
    account_element,
    active_sample,
    delivered_sample,
    pickup_sample,
    windowed_sample,
)

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "status",
    "raw_status",
    "delivered",
    "delivered_at",
    "planned_from",
    "planned_to",
    "pickup",
    "pickup_point",
    "url",
    "weight",
    "dimensions",
    "history",
    "raw",
]

_ZURICH = ZoneInfo("Europe/Zurich")
_RANGE = {"from": 1793055600000}


def _account(**kwargs):
    element = account_element(**kwargs)
    element["deliveryAddress"]["timeRange"] = dict(_RANGE)
    return lambda: normalize_account_parcel(element)


SAMPLES = {
    "tracked_active": lambda: normalize_parcel(active_sample()),
    "tracked_window": lambda: normalize_parcel(windowed_sample()),
    "tracked_delivered": lambda: normalize_parcel(delivered_sample()),
    "tracked_pickup": lambda: normalize_parcel(pickup_sample()),
    "account_active": _account(),
    "account_delivered": _account(status="DELIVERED", complete=True),
    "account_at_pickup": _account(status="WAITING_FOR_PICKUP", pickup_office="Bern 1"),
    "account_collected": _account(
        status="DELIVERED", complete=True, pickup_office="Bern 1"
    ),
}


@pytest.fixture(params=SAMPLES)
def parcel(request) -> dict:
    return SAMPLES[request.param]()


def test_keys_and_order(parcel):
    assert list(parcel) == CANONICAL_KEYS


def test_delivered_follows_status(parcel):
    assert parcel["delivered"] is (parcel["status"] is ParcelStatus.DELIVERED)


def test_delivered_at_only_when_delivered(parcel):
    if not parcel["delivered"]:
        assert parcel["delivered_at"] is None


def test_planned_window_is_ordered_and_anchored(parcel):
    if parcel["planned_to"] is not None:
        assert parcel["planned_from"] is not None
        assert datetime.fromisoformat(parcel["planned_to"]) > datetime.fromisoformat(
            parcel["planned_from"]
        )


def test_planned_is_empty_when_the_parcel_is_done_or_waiting(parcel):
    if parcel["status"] in (
        ParcelStatus.DELIVERED,
        ParcelStatus.AT_PICKUP_POINT,
        ParcelStatus.RETURNING,
    ):
        assert parcel["planned_from"] is None
        assert parcel["planned_to"] is None


def test_pickup_is_a_bool_and_the_point_needs_it(parcel):
    assert isinstance(parcel["pickup"], bool)
    if not parcel["pickup"]:
        assert parcel["pickup_point"] is None


def test_a_collected_account_parcel_stays_pickup_with_its_office():
    parcel = SAMPLES["account_collected"]()
    assert parcel["delivered"] is True
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] == "Bern 1"


def test_an_account_pickup_parcel_reaches_the_pickup_status():
    assert SAMPLES["account_at_pickup"]()["status"] is ParcelStatus.AT_PICKUP_POINT


def test_a_day_estimate_covers_the_whole_swiss_day():
    parcel = SAMPLES["tracked_active"]()
    assert parcel["planned_from"] == "2026-04-16T00:00:00+02:00"
    assert parcel["planned_to"] == "2026-04-16T23:59:59+02:00"


def test_an_account_time_range_ends_on_the_swiss_day():
    parcel = SAMPLES["account_active"]()
    assert parcel["planned_to"] == "2026-10-27T23:59:59+01:00"


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (
            "2026-04-29T13:00:00+02:00",
            "2026-04-29T15:00:00+02:00",
            ("2026-04-29T13:00:00+02:00", "2026-04-29T15:00:00+02:00"),
        ),
        (
            "2026-04-29T13:00:00+02:00",
            None,
            ("2026-04-29T13:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            "2026-04-29T13:00:00+02:00",
            "2026-04-29T13:00:00+02:00",
            ("2026-04-29T13:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            "2026-04-29T13:00:00",
            None,
            ("2026-04-29T13:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            "2026-04-29",
            None,
            ("2026-04-29T00:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            date(2026, 4, 29),
            date(2026, 4, 29),
            ("2026-04-29T00:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            "2026-04-29",
            "2026-05-01",
            ("2026-04-29T00:00:00+02:00", "2026-05-01T23:59:59+02:00"),
        ),
        (
            "2026-04-29T15:00:00+02:00",
            "2026-04-29T13:00:00+02:00",
            ("2026-04-29T13:00:00+02:00", "2026-04-29T15:00:00+02:00"),
        ),
        (
            None,
            "2026-04-29T13:00:00+02:00",
            ("2026-04-29T13:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            datetime(2026, 4, 29, 13, 0),
            datetime(2026, 4, 29, 15, 0),
            ("2026-04-29T13:00:00+02:00", "2026-04-29T15:00:00+02:00"),
        ),
        (None, None, (None, None)),
        ("garbage", None, (None, None)),
        (12, 13, (None, None)),
    ],
)
def test_planned_window_shapes(start, end, expected):
    assert planned_window(start, end, zone=_ZURICH) == expected


@pytest.mark.parametrize(
    "status",
    [ParcelStatus.AT_PICKUP_POINT, ParcelStatus.RETURNING, ParcelStatus.DELIVERED],
)
def test_planned_for_status_empties_the_window(status):
    assert planned_for_status(status, "a", "b") == (None, None)


def test_planned_for_status_keeps_the_window_otherwise():
    for status in (
        ParcelStatus.REGISTERED,
        ParcelStatus.IN_TRANSIT,
        ParcelStatus.OUT_FOR_DELIVERY,
        ParcelStatus.PROBLEM,
        ParcelStatus.UNKNOWN,
    ):
        assert planned_for_status(status, "a", "b") == ("a", "b")


def test_is_pickup_sources():
    assert is_pickup(True, ParcelStatus.IN_TRANSIT) is True
    assert is_pickup(None, ParcelStatus.AT_PICKUP_POINT) is True
    assert is_pickup(None, ParcelStatus.DELIVERED, [None, ParcelStatus.AT_PICKUP_POINT])
    assert is_pickup(False, ParcelStatus.IN_TRANSIT, [ParcelStatus.IN_TRANSIT]) is False
    assert is_pickup(None, ParcelStatus.DELIVERED) is False


def test_the_delivered_flag_does_not_turn_a_return_into_a_delivery():
    raw = delivered_sample()
    raw["globalStatus"] = "RETURNED"
    parcel = normalize_parcel(raw)
    assert parcel["status"] is ParcelStatus.RETURNING
    assert parcel["delivered"] is False
    assert parcel["planned_from"] is None
