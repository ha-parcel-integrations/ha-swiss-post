"""Canonical mapping for the public tracking surfaces (ekp-web + eosapi).

Carrier-specific here: :data:`_STATUS_MAP`, :func:`normalize_parcel` and the
field lookups in :func:`build_history`. The timestamp parsing, sort contract,
delivered filter and one-shot warning machinery are shared and imported from
:mod:`..parcels`.

The payload this maps is surface A's shipment dict, with surface B's event
timeline merged into its ``events`` key by :mod:`.api`.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from ..const import HISTORY_MAX_EVENTS, TRACKING_URL, ParcelStatus
from ..parcels import (
    NEW_ISSUE_URL,
    format_dimensions,
    parse_iso,
    to_iso_timestamp,
    warn_once,
)

_LOGGER = logging.getLogger(__name__)

# Swiss Post's ``globalStatus`` is the mapping field — never the per-event
# ``Status`` from the timeline (see :func:`build_history`), and never the
# product-scoped ``status`` code, whose vocabulary we cannot read.
#
# Only ``DELIVERED`` and ``RETURNED`` have been seen in live data; the rest come
# from a third-party client's map and are unverified. Note the gap: **no
# pickup-point token is known** on this surface, even though Swiss Post clearly
# has the concept (``deliveryPostOfficeZip``, ``avis``). The account surface's
# ``WAITING_FOR_PICKUP`` is the token this one lacks.
_STATUS_MAP: dict[str, ParcelStatus] = {
    "REGISTERED": ParcelStatus.REGISTERED,
    "CUSTOMS": ParcelStatus.IN_TRANSIT,
    "TO_BE_DELIVERED": ParcelStatus.IN_TRANSIT,
    "IN_DELIVERY": ParcelStatus.OUT_FOR_DELIVERY,
    "DELIVERED": ParcelStatus.DELIVERED,
    "MISSED_DELIVERY": ParcelStatus.PROBLEM,
    "NOT_DELIVERED": ParcelStatus.PROBLEM,
    "RETURNED": ParcelStatus.RETURNING,
}


def map_parcel_status(code: str | None) -> ParcelStatus:
    """Map a ``globalStatus`` code to a canonical :class:`ParcelStatus`.

    ``None`` (a not-yet-scanned parcel) reports ``unknown`` silently; an
    unrecognised code reports ``unknown`` with a one-shot warning.
    """
    if not code:
        return ParcelStatus.UNKNOWN
    mapped = _STATUS_MAP.get(code)
    if mapped is not None:
        return mapped
    warn_once(
        f"tracking_status:{code}",
        _LOGGER,
        "Unrecognised Swiss Post status — help us map it. Open an issue and "
        "paste this line: %s\n  status=%s → reported as 'unknown'",
        NEW_ISSUE_URL,
        code,
    )
    return ParcelStatus.UNKNOWN


def build_history(
    events: list | None, *, max_events: int = HISTORY_MAX_EVENTS
) -> list[dict]:
    """Build the canonical ``history`` list from surface B's ``History`` list.

    Each entry is ``{timestamp, status, raw_status}``. The per-event ``Status``
    is deliberately **not** mapped: on a real delivered parcel four of five
    events carried the same ``PST`` value — including the delivery itself — so
    it is not a status vocabulary. Every entry keeps ``status: null`` and the
    human-readable ``Description`` as ``raw_status``. Sorted oldest → newest and
    capped to the most recent ``max_events``.
    """
    parseable: list[tuple[datetime, dict]] = []
    unparseable: list[dict] = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        timestamp = to_iso_timestamp(event.get("TimeStamp"))
        if not timestamp:
            continue
        entry = {
            "timestamp": timestamp,
            "status": None,
            "raw_status": event.get("Description") or event.get("Status"),
        }
        parsed = parse_iso(timestamp)
        if parsed is None:
            unparseable.append(entry)
        else:
            parseable.append((parsed, entry))
    parseable.sort(key=lambda item: item[0])
    ordered = [entry for _, entry in parseable] + unparseable
    return ordered[-max_events:]


def tracking_url(tracking_code: str | None) -> str | None:
    """Construct the consumer tracking deep-link for a parcel."""
    if not tracking_code:
        return None
    return TRACKING_URL.format(tracking_code=tracking_code)


def _mm_to_cm(value: Any) -> float | None:
    """Convert one millimetre measurement to centimetres."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return value / 10


def _dimensions_cm(properties: dict) -> dict[str, Any] | None:
    """Return the canonical dimensions from ``dimension1/2/3``.

    **The three numbers carry no axis semantics.** Swiss Post's own tracking
    frontend sorts them before labelling anything
    (``[d1, d2, d3].map(v => v / 10).sort((a, b) => a - b)``), so their position
    in the payload is meaningless and must not be read as length/width/height.

    We sort as well and label largest → length, middle → width, smallest →
    height, which keeps the suite's usual ``length >= width`` convention. Swiss
    Post's frontend assigns the largest to *width* and the middle to *length*
    instead; the three values are identical either way, only the two larger
    labels differ.
    """
    values = [_mm_to_cm(properties.get(f"dimension{index}")) for index in (1, 2, 3)]
    if any(value is None for value in values):
        return None
    height, width, length = sorted(values)
    return format_dimensions(length, width, height)


def _receiver(raw: dict) -> str | None:
    """Return the recipient as "zip city".

    Swiss Post blanks the recipient's ``name*`` fields on this surface, so the
    town is all there is — which is also all we want on a dashboard.
    """
    addressee = raw.get("addressee") or {}
    parts = [
        str(part).strip()
        for part in (addressee.get("zip"), addressee.get("city"))
        if part
    ]
    return " ".join(parts) or None


def _delivery_window(raw: dict) -> tuple[str | None, str | None]:
    """Return ``(planned_from, planned_to)`` from the shipment's ETA fields.

    ``deliveryRange`` is the real window and takes precedence;
    ``calculatedDeliveryDate`` is the day-level estimate every parcel carries.
    Only ``calculatedDeliveryDate`` has ever been seen populated, so a parcel
    that does carry a window is worth a one-shot report.
    """
    windowed = [
        field
        for field in ("deliveryRange", "deliveryTimeWindow", "deliveryTimeInterval")
        if raw.get(field)
    ]
    if windowed:
        warn_once(
            "tracking_delivery_window",
            _LOGGER,
            "Swiss Post reported a delivery window (%s) on a parcel — the first "
            "time we have seen one. Please check the expected delivery time, and "
            "report it: %s",
            ", ".join(windowed),
            NEW_ISSUE_URL,
        )

    delivery_range = raw.get("deliveryRange") or {}
    planned_from = to_iso_timestamp(
        delivery_range.get("start") or raw.get("calculatedDeliveryDate")
    )
    planned_to = to_iso_timestamp(delivery_range.get("end"))
    if planned_from and planned_to and parse_iso(planned_to) == parse_iso(planned_from):
        # Same instant twice is a point estimate, not a window.
        planned_to = None
    return planned_from, planned_to


def normalize_parcel(raw: dict, *, include_history: bool = False) -> dict:
    """Return a carrier-agnostic parcel dict with the payload under ``raw``.

    The **keys of the returned dict are the contract**: every carrier in the
    suite returns exactly these, in this order. A key Swiss Post does not
    expose is ``None`` — never omitted.
    """
    tracking_code = raw.get("shipmentNumber")
    status_code = raw.get("globalStatus")
    status = map_parcel_status(status_code)
    # Prefer the payload's own boolean over the status enum: it is explicit,
    # and it survives an unmapped status token.
    delivered = bool(raw.get("delivered")) or status is ParcelStatus.DELIVERED

    planned_from, planned_to = _delivery_window(raw)

    pickup_fields = [
        field
        for field in ("deliveryPostOfficeZip", "avis", "displayedAvisCode")
        if raw.get(field)
    ]
    if pickup_fields:
        warn_once(
            "tracking_pickup_fields",
            _LOGGER,
            "Swiss Post reported pickup-point fields (%s) on a parcel — the "
            "first time we have seen them. The status value that comes with "
            "them may not be mapped yet. Please report it: %s",
            ", ".join(pickup_fields),
            NEW_ISSUE_URL,
        )
    pickup = not delivered and (
        status is ParcelStatus.AT_PICKUP_POINT or bool(pickup_fields)
    )
    office_zip = raw.get("deliveryPostOfficeZip")

    properties = raw.get("physicalProperties") or {}
    weight = properties.get("weight")
    dimensions = _dimensions_cm(properties)

    return {
        "carrier": "Swiss Post",
        "barcode": tracking_code,
        "sender": raw.get("sender") or None,
        "receiver": _receiver(raw),
        "status": status,
        "raw_status": raw.get("status") or status_code,
        "delivered": delivered,
        "delivered_at": to_iso_timestamp(raw.get("deliveryDate")) if delivered else None,
        "planned_from": None if delivered else planned_from,
        "planned_to": None if delivered else planned_to,
        "pickup": pickup,
        "pickup_point": str(office_zip) if pickup and office_zip else None,
        "url": tracking_url(tracking_code),
        # Grams → kilograms, rounded so a sensor state is not 1.1400000000000001.
        "weight": round(weight / 1000, 3) if isinstance(weight, (int, float)) else None,
        "dimensions": dimensions,
        "history": build_history(raw.get("events")) if include_history else None,
        "raw": raw,
    }
