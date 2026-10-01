"""Tests for Swiss Post sensor property logic."""
from datetime import datetime, timezone
from unittest.mock import MagicMock

from custom_components.swiss_post.const import ParcelStatus
from custom_components.swiss_post.sensor import (
    SwissPostDeliveredParcelsSensor,
    SwissPostIncomingParcelsSensor,
    SwissPostLastUpdateSensor,
    SwissPostNextDeliverySensor,
    SwissPostParcelSensor,
)


def _entry(entry_id: str = "e1") -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    return entry


def _coordinator(data: list[dict], delivered: list[dict] | None = None) -> MagicMock:
    coordinator = MagicMock()
    coordinator.data = data
    coordinator.delivered = delivered if delivered is not None else []
    return coordinator


def _parcel(
    barcode: str,
    status: ParcelStatus = ParcelStatus.IN_TRANSIT,
    pickup: bool = False,
    planned_from: str | None = None,
) -> dict:
    return {
        "carrier": "Swiss Post",
        "barcode": barcode,
        "sender": "Sender",
        "receiver": None,
        "status": status,
        "pickup": pickup,
        "planned_from": planned_from,
    }


def test_incoming_counts_and_lists():
    coordinator = _coordinator([_parcel("A"), _parcel("B")])
    sensor = SwissPostIncomingParcelsSensor(coordinator, _entry(), lambda _: None, set())
    assert sensor.native_value == 2
    assert len(sensor.extra_state_attributes["parcels"]) == 2


def test_parcel_sensor_status_and_attributes():
    parcel = _parcel("A", status=ParcelStatus.OUT_FOR_DELIVERY)
    sensor = SwissPostParcelSensor(_coordinator([parcel]), _entry(), "A")
    assert sensor.native_value == ParcelStatus.OUT_FOR_DELIVERY
    assert sensor.extra_state_attributes["barcode"] == "A"


def test_parcel_sensor_missing_barcode():
    sensor = SwissPostParcelSensor(_coordinator([_parcel("A")]), _entry(), "OTHER")
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {}


def test_next_delivery_picks_earliest():
    coordinator = _coordinator([
        _parcel("A", planned_from="2026-05-02T10:00:00Z"),
        _parcel("B", planned_from="2026-05-01T10:00:00Z"),
    ])
    sensor = SwissPostNextDeliverySensor(coordinator, _entry())
    assert sensor.native_value == datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc)
    assert sensor.extra_state_attributes["barcode"] == "B"


def test_next_delivery_none_without_moments():
    sensor = SwissPostNextDeliverySensor(_coordinator([_parcel("A")]), _entry())
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {}


def test_next_delivery_skips_unparseable_moment():
    coordinator = _coordinator([
        _parcel("A", planned_from="not-a-date"),
        _parcel("B", planned_from="2026-05-01T10:00:00Z"),
    ])
    sensor = SwissPostNextDeliverySensor(coordinator, _entry())
    assert sensor.extra_state_attributes["barcode"] == "B"


def test_delivered_sensor():
    coordinator = _coordinator([], delivered=[_parcel("D", status=ParcelStatus.DELIVERED)])
    sensor = SwissPostDeliveredParcelsSensor(coordinator, _entry())
    assert sensor.native_value == 1
    assert sensor.extra_state_attributes["parcels"][0]["barcode"] == "D"


def test_last_update_sensor():
    coordinator = _coordinator([])
    moment = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    coordinator.last_success_time = moment
    sensor = SwissPostLastUpdateSensor(coordinator, _entry())
    assert sensor.native_value == moment


# ---------------------------------------------------------------------------
# Account source: the coordinator publishes a four-way dict instead of a list,
# and two extra sender-side sensors exist.
# ---------------------------------------------------------------------------


def _account_coordinator(**buckets) -> MagicMock:
    coordinator = MagicMock()
    coordinator.data = {
        "incoming_active": [],
        "incoming_delivered": [],
        "outgoing_active": [],
        "outgoing_delivered": [],
        **buckets,
    }
    return coordinator


def test_bucket_reads_both_coordinator_shapes():
    """The entities must not care which source they are attached to."""
    from custom_components.swiss_post.sensor import _bucket

    tracking = _coordinator([_parcel("A")], delivered=[_parcel("B")])
    assert [p["barcode"] for p in _bucket(tracking, "incoming_active")] == ["A"]
    assert [p["barcode"] for p in _bucket(tracking, "incoming_delivered")] == ["B"]
    # A tracking hub has no sender side at all.
    assert _bucket(tracking, "outgoing_active") == []

    account = _account_coordinator(outgoing_active=[_parcel("C")])
    assert [p["barcode"] for p in _bucket(account, "outgoing_active")] == ["C"]
    assert _bucket(account, "incoming_active") == []


def test_incoming_sensor_reads_the_account_dict():
    coordinator = _account_coordinator(incoming_active=[_parcel("A"), _parcel("B")])
    sensor = SwissPostIncomingParcelsSensor(coordinator, _entry(), lambda _: None, set())
    assert sensor.native_value == 2


def test_outgoing_sensors_count_sender_parcels():
    from custom_components.swiss_post.sensor import (
        SwissPostOutgoingDeliveredParcelsSensor,
        SwissPostOutgoingParcelsSensor,
    )

    coordinator = _account_coordinator(
        outgoing_active=[_parcel("A")],
        outgoing_delivered=[_parcel("B"), _parcel("C")],
    )
    active = SwissPostOutgoingParcelsSensor(coordinator, _entry())
    done = SwissPostOutgoingDeliveredParcelsSensor(coordinator, _entry())
    assert active.native_value == 1
    assert [p["barcode"] for p in active.extra_state_attributes["parcels"]] == ["A"]
    assert done.native_value == 2
    assert len(done.extra_state_attributes["parcels"]) == 2


def test_delivered_sensor_reads_the_account_dict():
    coordinator = _account_coordinator(incoming_delivered=[_parcel("A")])
    sensor = SwissPostDeliveredParcelsSensor(coordinator, _entry())
    assert sensor.native_value == 1


def test_every_sensor_has_an_icon_and_a_consistent_unit():
    """Guard the two things a new sensor is easy to forget.

    A missing ``icons.json`` entry leaves the sensor with HA's generic fallback
    icon, and a unit copied from English leaves a Dutch dashboard reading
    "pakketten" on one tile and "parcels" on the next.
    """
    import json
    from pathlib import Path

    root = Path("custom_components/swiss_post")
    icons = json.loads((root / "icons.json").read_text())["entity"]["sensor"]
    strings = json.loads((root / "strings.json").read_text())["entity"]["sensor"]

    # Every translated sensor needs an icon.
    assert set(strings) - set(icons) == set()

    for path in sorted((root / "translations").glob("*.json")):
        sensors = json.loads(path.read_text())["entity"]["sensor"]
        # Same sensor set as strings.json, so no language misses one.
        assert set(sensors) == set(strings), path.name
        units = {
            value["unit_of_measurement"]
            for value in sensors.values()
            if "unit_of_measurement" in value
        }
        assert len(units) == 1, f"{path.name} mixes units: {units}"
