"""Tests for the pure parcel-mapping helpers.

These need no Home Assistant instance — the whole point of keeping
``parcels.py`` free of I/O is that the carrier-specific mapping (the part you
rewrite per carrier) can be tested as plain functions.
"""
from datetime import datetime, timedelta, timezone

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.swiss_post import parcels as parcels_module
from custom_components.swiss_post.const import (
    CAPABILITIES,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DOMAIN,
    KNOWN_CAPABILITIES,
    ParcelStatus,
)
from custom_components.swiss_post.parcels import (
    apply_delivered_filter,
    build_history,
    format_dimensions,
    map_parcel_status,
    normalize_parcel,
    parse_iso,
    sort_parcels_by_ts,
    to_iso_timestamp,
)

from .payloads import (
    ACTIVE_CODE,
    DELIVERED_CODE,
    EVENTS,
    active_sample,
    delivered_sample,
    event,
    pickup_sample,
    windowed_sample,
)


@pytest.fixture(autouse=True)
def _reset_one_shot_logs():
    """Clear the module's one-shot log state between tests."""
    parcels_module._unmapped_statuses_logged.clear()
    parcels_module._reported_once.clear()
    yield


# ---------------------------------------------------------------------------
# map_parcel_status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code,expected",
    [
        ("REGISTERED", ParcelStatus.REGISTERED),
        ("CUSTOMS", ParcelStatus.IN_TRANSIT),
        ("TO_BE_DELIVERED", ParcelStatus.IN_TRANSIT),
        ("IN_DELIVERY", ParcelStatus.OUT_FOR_DELIVERY),
        ("DELIVERED", ParcelStatus.DELIVERED),
        ("MISSED_DELIVERY", ParcelStatus.PROBLEM),
        ("NOT_DELIVERED", ParcelStatus.PROBLEM),
        ("RETURNED", ParcelStatus.RETURNING),
    ],
)
def test_map_parcel_status_known(code, expected):
    assert map_parcel_status(code) == expected


def test_map_parcel_status_missing_is_unknown():
    assert map_parcel_status(None) == ParcelStatus.UNKNOWN
    assert map_parcel_status("") == ParcelStatus.UNKNOWN


def test_map_parcel_status_unmapped_is_unknown():
    assert map_parcel_status("TELEPORTED") == ParcelStatus.UNKNOWN


def test_unmapped_status_warns_only_once(caplog):
    assert map_parcel_status("ABDUCTED") == ParcelStatus.UNKNOWN
    assert map_parcel_status("ABDUCTED") == ParcelStatus.UNKNOWN
    assert caplog.text.count("ABDUCTED") == 1
    assert "issues/new" in caplog.text


# ---------------------------------------------------------------------------
# timestamp helpers
# ---------------------------------------------------------------------------


def test_parse_iso_handles_z_naive_and_garbage():
    assert parse_iso("2026-04-16T10:28:29Z").tzinfo is not None
    # A naive value is assumed UTC so mixed lists still sort.
    assert parse_iso("2026-04-16T10:28:29").tzinfo == timezone.utc
    assert parse_iso("not-a-date") is None
    assert parse_iso(None) is None


def test_to_iso_timestamp_normalises_both_surfaces():
    """ekp-web stamps with an offset, the timeline in UTC — both come out aware."""
    assert to_iso_timestamp("2026-04-16T10:28:29+02:00") == "2026-04-16T10:28:29+02:00"
    assert to_iso_timestamp("2026-04-16T08:28:29Z") == "2026-04-16T08:28:29+00:00"
    assert to_iso_timestamp(None) is None
    # Unparseable values pass through rather than vanishing.
    assert to_iso_timestamp("not-a-date") == "not-a-date"


def test_format_dimensions_needs_all_three_axes():
    assert format_dimensions(30, 20, 10) == {
        "length": 30,
        "width": 20,
        "height": 10,
        "text": "30 x 20 x 10 cm",
    }
    assert format_dimensions(30, None, 10) is None


# ---------------------------------------------------------------------------
# build_history
# ---------------------------------------------------------------------------


def test_build_history_orders_oldest_to_newest():
    history = build_history(EVENTS)
    assert len(history) == 5
    assert history[0]["raw_status"] == "Dateneinlieferung durch Aufgeber"
    assert history[-1]["raw_status"] == "Zugestellt an Domiziladresse"


def test_build_history_never_maps_the_event_status():
    """Surface B's ``Status`` is near-constant — four of five events on a real
    delivered parcel read ``PST``, including the delivery itself. Mapping it
    would file delivered parcels as in transit."""
    assert all(entry["status"] is None for entry in build_history(EVENTS))


def test_build_history_caps_to_max_events():
    events = [
        event("PST", f"2026-04-{day:02d}T10:00:00Z", "Sortiert", day)
        for day in range(1, 26)
    ]
    assert len(build_history(events, max_events=20)) == 20


def test_build_history_handles_missing_and_malformed():
    assert build_history(None) == []
    assert build_history([{"Status": "PST"}]) == []  # no timestamp
    assert build_history(["not-a-dict"]) == []


def test_build_history_keeps_unparseable_timestamp_last():
    history = build_history(
        [
            event("PST", "2026-04-16T10:00:00Z", "fine"),
            event("PST", "not-a-date", "odd"),
        ]
    )
    assert [entry["raw_status"] for entry in history] == ["fine", "odd"]


def test_build_history_falls_back_to_the_event_code_without_text():
    history = build_history([event("NTF", "2026-04-16T10:00:00Z", "")])
    assert history[0]["raw_status"] == "NTF"


# ---------------------------------------------------------------------------
# normalize_parcel — the canonical contract
# ---------------------------------------------------------------------------

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


def test_normalize_publishes_exactly_the_canonical_keys():
    """The aggregator and cross-carrier dashboards depend on this key set."""
    assert list(normalize_parcel(delivered_sample())) == CANONICAL_KEYS


def test_normalize_delivered_parcel():
    parcel = normalize_parcel(delivered_sample())
    assert parcel["carrier"] == "Swiss Post"
    assert parcel["barcode"] == DELIVERED_CODE
    assert parcel["sender"] is None  # never populated on this surface
    assert parcel["receiver"] == "3000 Bern"
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["raw_status"] == "PARCEL.*.1.6"
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-04-16T10:28:29+02:00"
    # A delivered parcel drops its ETA — the window is meaningless once it has
    # arrived.
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None
    assert parcel["pickup"] is False
    assert parcel["pickup_point"] is None
    assert parcel["url"].endswith(f"/entry/search/{DELIVERED_CODE}")
    assert parcel["history"] is None  # opt-in, default off


def test_normalize_converts_weight_and_dimensions_to_the_canonical_units():
    """The payload is grams and millimetres; the contract is kg and cm."""
    parcel = normalize_parcel(delivered_sample())
    assert parcel["weight"] == 1.14
    assert parcel["dimensions"] == {
        "length": 40.0,
        "width": 25.0,
        "height": 15.5,
        "text": "40 x 25 x 15 cm",
    }


def test_normalize_warns_once_that_the_dimension_order_is_assumed(caplog):
    normalize_parcel(delivered_sample())
    normalize_parcel(delivered_sample())
    assert caplog.text.count("length, width, height") == 1
    assert "issues/new" in caplog.text


def test_normalize_survives_a_payload_without_physical_properties():
    raw = delivered_sample()
    raw["physicalProperties"] = None
    parcel = normalize_parcel(raw)
    assert parcel["weight"] is None
    assert parcel["dimensions"] is None


def test_normalize_delivered_flag_comes_from_the_payload_boolean():
    """The boolean survives a status token we have never seen; a match would not."""
    raw = delivered_sample()
    raw["globalStatus"] = "SOMETHING_NEW"
    parcel = normalize_parcel(raw)
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-04-16T10:28:29+02:00"


def test_normalize_active_parcel_uses_the_calculated_delivery_date():
    parcel = normalize_parcel(active_sample())
    assert parcel["status"] == ParcelStatus.OUT_FOR_DELIVERY
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None
    assert parcel["planned_from"] == "2026-04-16T00:00:00+02:00"
    assert parcel["planned_to"] is None  # a day estimate, not a window


def test_normalize_prefers_a_real_delivery_window(caplog):
    parcel = normalize_parcel(windowed_sample())
    assert parcel["planned_from"] == "2026-04-16T13:00:00+02:00"
    assert parcel["planned_to"] == "2026-04-16T15:00:00+02:00"
    # Never seen live — the first user who gets one should tell us.
    assert "delivery window" in caplog.text
    assert "issues/new" in caplog.text


def test_normalize_collapses_point_estimate_to_no_window_end():
    raw = windowed_sample()
    raw["deliveryRange"]["end"] = raw["deliveryRange"]["start"]
    parcel = normalize_parcel(raw)
    assert parcel["planned_from"] == "2026-04-16T13:00:00+02:00"
    assert parcel["planned_to"] is None


def test_normalize_pickup_parcel_is_inferred_from_the_post_office_fields(caplog):
    """No globalStatus token for "waiting at a post office" is known, so the
    post-office fields are what flags it — and the parcel is worth reporting."""
    parcel = normalize_parcel(pickup_sample())
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] == "3011"
    assert "pickup-point fields" in caplog.text
    # Field names, never their values — a pickup point says where someone lives.
    assert "3011" not in caplog.text


def test_normalize_delivered_parcel_is_never_at_a_pickup_point():
    raw = delivered_sample()
    raw["deliveryPostOfficeZip"] = "3011"
    parcel = normalize_parcel(raw)
    assert parcel["pickup"] is False
    assert parcel["pickup_point"] is None


def test_normalize_pending_placeholder():
    """A tracked-but-not-yet-scanned code still yields a full parcel dict."""
    parcel = normalize_parcel({"shipmentNumber": ACTIVE_CODE})
    assert parcel["barcode"] == ACTIVE_CODE
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["delivered"] is False
    assert parcel["raw_status"] is None
    assert parcel["receiver"] is None
    assert parcel["weight"] is None
    assert parcel["dimensions"] is None
    assert parcel["history"] is None


def test_normalize_without_a_tracking_number_has_no_deep_link():
    assert normalize_parcel({})["url"] is None


def test_normalize_history_is_opt_in():
    raw = delivered_sample()
    raw["events"] = EVENTS
    assert normalize_parcel(raw)["history"] is None
    history = normalize_parcel(raw, include_history=True)["history"]
    assert len(history) == 5
    # UTC event stamps are normalised like every other timestamp.
    assert history[-1]["timestamp"] == "2026-04-16T08:28:29+00:00"


def test_normalize_falls_back_to_the_global_status_without_a_product_code():
    raw = active_sample()
    raw["status"] = None
    assert normalize_parcel(raw)["raw_status"] == "IN_DELIVERY"


def test_normalize_keeps_raw_payload():
    raw = active_sample()
    assert normalize_parcel(raw)["raw"] is raw


# ---------------------------------------------------------------------------
# sort_parcels_by_ts
# ---------------------------------------------------------------------------


def test_sort_parcels_ascending_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "planned_from": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "planned_from": None},
        {"barcode": "c", "planned_from": "2026-05-01T10:00:00Z"},
    ]
    ordered = [p["barcode"] for p in sort_parcels_by_ts(parcels, "planned_from")]
    assert ordered == ["c", "a", "b"]


def test_sort_parcels_descending_still_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "delivered_at": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "delivered_at": "nonsense"},
        {"barcode": "c", "delivered_at": "2026-05-01T10:00:00Z"},
    ]
    ordered = [
        p["barcode"]
        for p in sort_parcels_by_ts(parcels, "delivered_at", descending=True)
    ]
    assert ordered == ["a", "c", "b"]


# ---------------------------------------------------------------------------
# apply_delivered_filter
# ---------------------------------------------------------------------------


def _entry(filter_type: str, amount: int) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_DELIVERED_FILTER_TYPE: filter_type,
            CONF_DELIVERED_FILTER_AMOUNT: amount,
        },
        unique_id=DOMAIN,
    )


def _delivered_pair() -> list[dict]:
    now = datetime.now(timezone.utc)
    return [
        {"barcode": "RECENT", "delivered_at": (now - timedelta(days=1)).isoformat()},
        {"barcode": "OLD", "delivered_at": (now - timedelta(days=30)).isoformat()},
    ]


def test_delivered_filter_by_days():
    kept = apply_delivered_filter(_delivered_pair(), _entry("days", 7))
    assert [p["barcode"] for p in kept] == ["RECENT"]


def test_delivered_filter_by_count():
    parcels = _delivered_pair()
    assert apply_delivered_filter(parcels, _entry("parcels", 1)) == parcels[:1]


def test_delivered_filter_keeps_unparseable_timestamp():
    """Better to show a parcel with a broken date than to silently drop it."""
    parcels = [{"barcode": "WEIRD", "delivered_at": "nonsense"}]
    assert apply_delivered_filter(parcels, _entry("days", 7)) == parcels


def test_capabilities_are_known_values():
    """A typo here would silently misreport this carrier on the docs site."""
    assert CAPABILITIES <= KNOWN_CAPABILITIES


def test_capabilities_cover_every_known_field():
    """Swiss Post's merged two-host payload is the one carrier that fills all six."""
    assert CAPABILITIES == KNOWN_CAPABILITIES
