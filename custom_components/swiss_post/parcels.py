"""Canonical parcel shape, status mapping and list helpers.

Everything in this module is a **pure function** — no I/O, no Home Assistant
objects beyond the config entry's options. That is deliberate: it keeps the
carrier-specific mapping (which you rewrite per carrier) apart from the
coordinator (which is nearly identical everywhere), and it makes the mapping
trivially unit-testable without spinning up HA.

Carrier-specific here: :data:`_STATUS_MAP`, :func:`normalize_parcel` and the
field lookups in :func:`build_history`. Everything else — the timestamp
parsing, the sort contract, the delivered filter, the one-shot warning for
unmapped statuses — is suite-wide machinery and should be left alone.

The payload this maps is surface A's shipment dict, with surface B's event
timeline merged into its ``events`` key by :mod:`.api`.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    HISTORY_MAX_EVENTS,
    TRACKING_URL,
    ParcelStatus,
)

_LOGGER = logging.getLogger(__name__)

# Where users report a status we do not map yet. Rewritten by the bootstrap
# script; it must point at the carrier's own repo so the log line is
# copy-pasteable straight into a new issue.
#
# The ``?template=`` parameter matters: without it the link opens a blank form,
# and the report comes back missing the version and the log line we need.
NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-swiss-post/issues/new"
    "?template=unrecognised_status.yml"
)

# Swiss Post's ``globalStatus`` is the mapping field — never the per-event
# ``Status`` from the timeline (see :func:`build_history`), and never the
# product-scoped ``status`` code, whose vocabulary we cannot read.
#
# Only ``DELIVERED`` has been seen in live data; the rest come from a
# third-party client's map and are unverified. Note the gap: **no pickup-point
# token is known**, even though Swiss Post clearly has the concept
# (``deliveryPostOfficeZip``, ``avis``). Expect the first parcel routed to a
# post office to land here as ``unknown`` plus a one-shot warning — that is how
# the map grows.
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

# Status codes we have already warned about, so each unmapped one is logged
# only once per HA session instead of on every poll.
_unmapped_statuses_logged: set[str] = set()

# Pre-1.0 "we guessed this" reports already made, keyed by topic, so each fires
# at most once per HA session. See :func:`_warn_report_once`.
_reported_once: set[str] = set()


def _warn_unmapped_status(code: str) -> None:
    """Log an unmapped carrier status once, with a copy-paste issue link."""
    if code in _unmapped_statuses_logged:
        return
    _unmapped_statuses_logged.add(code)
    _LOGGER.warning(
        "Unrecognised Swiss Post status — help us map it. Open an issue "
        "and paste this line: %s\n  status=%s → reported as 'unknown'",
        NEW_ISSUE_URL,
        code,
    )


def map_parcel_status(code: str | None) -> ParcelStatus:
    """Map a carrier status code to a canonical :class:`ParcelStatus`.

    ``None`` (a not-yet-scanned parcel) reports ``unknown`` silently; an
    unrecognised code reports ``unknown`` with a one-shot warning.
    """
    if not code:
        return ParcelStatus.UNKNOWN
    mapped = _STATUS_MAP.get(code)
    if mapped is not None:
        return mapped
    _warn_unmapped_status(code)
    return ParcelStatus.UNKNOWN


def _warn_report_once(topic: str, message: str, *args: Any) -> None:
    """Log one of the pre-1.0 "we guessed this" warnings, once per HA session.

    Swiss Post's mapping was built from a single live parcel — delivered,
    domestic, home address. The three shapes below have never been seen
    populated, so the first user who hits one is the only way to confirm them.
    Field *names* are logged, never their values: a pickup point or a delivery
    window says where somebody lives and when they are getting a parcel.
    """
    if topic in _reported_once:
        return
    _reported_once.add(topic)
    _LOGGER.warning(message + " Please report it: %s", *args, NEW_ISSUE_URL)


def _warn_pickup_fields_seen(fields: list[str]) -> None:
    """Report the first parcel routed to a pickup point.

    No ``globalStatus`` value for "waiting at a post office" is known, so this
    parcel's status is probably being reported as ``unknown`` right next to
    this line — and that log pair is exactly what identifies the missing token.
    """
    _warn_report_once(
        "pickup_fields",
        "Swiss Post reported pickup-point fields (%s) on a parcel — the first "
        "time we have seen them. The status value that comes with them is not "
        "in our map yet, so it may show as 'unknown'.",
        ", ".join(fields),
    )


def _warn_delivery_window_seen(fields: list[str]) -> None:
    """Report the first parcel that carries a real delivery window."""
    _warn_report_once(
        "delivery_window",
        "Swiss Post reported a delivery window (%s) on a parcel — the first "
        "time we have seen one. Please check that the parcel's expected "
        "delivery time looks right.",
        ", ".join(fields),
    )


def _warn_dimensions_assumed() -> None:
    """Report the first parcel with dimensions, to settle the axis order."""
    _warn_report_once(
        "dimensions_order",
        "Swiss Post reports parcel dimensions as three unnamed numbers; we "
        "assume length, width, height in that order. If you know this parcel's "
        "real size, please check the dimensions shown against it.",
    )


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string to an aware datetime, or ``None`` on failure.

    Naive values are treated as UTC so a list always sorts without crashing on
    a mixed set.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def to_iso_timestamp(value: Any) -> str | None:
    """Return a normalised ISO 8601 string for an API timestamp field.

    Swiss Post stamps in two formats in one parcel: the shipment fields carry a
    local offset (``+02:00``) and the event timeline is UTC (``Z``). Both are
    re-emitted as aware ISO 8601 so a consumer never has to care which surface
    a timestamp came from. An unparseable value is passed through untouched
    rather than dropped — :func:`parse_iso` guards the consumers.
    """
    if value is None:
        return None
    text = str(value)
    parsed = parse_iso(text)
    return parsed.isoformat() if parsed is not None else text


def format_dimensions(
    length: float | None, width: float | None, height: float | None
) -> dict[str, Any] | None:
    """Return the canonical ``dimensions`` dict, or ``None`` when incomplete.

    Units contract: **centimetres**, with ``text`` pre-formatted as
    ``"L x W x H cm"`` (integer values, lowercase ``x``) so dashboards can show
    a dimension without doing their own formatting. Convert before calling if
    the carrier reports millimetres or inches.
    """
    if length is None or width is None or height is None:
        return None
    return {
        "length": length,
        "width": width,
        "height": height,
        "text": f"{int(length)} x {int(width)} x {int(height)} cm",
    }


def build_history(
    events: list | None, *, max_events: int = HISTORY_MAX_EVENTS
) -> list[dict]:
    """Build the canonical ``history`` list from the carrier's event list.

    Each entry is ``{timestamp, status, raw_status}`` — identical across all
    suite carriers, and top-level (not under ``raw``) so it survives the
    aggregator's ``strip_raw()``. ``raw_status`` is the carrier's own text, or
    its event code when the API has no human-readable text. Sorted oldest →
    newest and capped to the most recent ``max_events``.

    ``events`` here is surface B's ``History`` list, merged in by :mod:`.api`.
    Its per-event ``Status`` is deliberately **not** mapped: on a real
    delivered parcel four of five events carried the same ``PST`` value —
    including the delivery itself — so it is not a status vocabulary, and
    treating it as one mis-reports parcels. Canonical event statuses would have
    to come from a field Swiss Post does not expose, so every entry keeps
    ``status: null`` and the human-readable ``Description`` as ``raw_status``.
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
        _warn_delivery_window_seen(windowed)

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
    suite returns exactly these, in this order, and the aggregator and
    cross-carrier dashboards depend on it. A key Swiss Post does not expose is
    ``None`` — never omitted.

    Swiss Post specifics:

    * ``delivered`` comes from the payload's own boolean, not from a status
      match — it is the one field the API states outright, and it keeps working
      if a status token we have never seen shows up.
    * ``raw_status`` is the product-scoped ``status`` code (``PARCEL.*.1.6``);
      the API ships no human-readable status text on this surface.
    * ``pickup`` is inferred from the post-office fields as well as the status,
      because no ``globalStatus`` value for "waiting at a post office" is known
      yet. ``pickup_point`` is then the office's postcode — Swiss Post does not
      name it here.
    * ``sender`` is ``None`` on every parcel seen so far; the payload has a
      ``senderCountry`` but that is not a sender.
    * ``weight`` arrives in grams and the dimensions in millimetres; the
      canonical contract is kilograms and centimetres.
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
        _warn_pickup_fields_seen(pickup_fields)
    pickup = not delivered and (
        status is ParcelStatus.AT_PICKUP_POINT or bool(pickup_fields)
    )
    office_zip = raw.get("deliveryPostOfficeZip")

    properties = raw.get("physicalProperties") or {}
    weight = properties.get("weight")
    dimensions = format_dimensions(
        _mm_to_cm(properties.get("dimension1")),
        _mm_to_cm(properties.get("dimension2")),
        _mm_to_cm(properties.get("dimension3")),
    )
    if dimensions is not None:
        _warn_dimensions_assumed()

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
        # Grams → kilograms. Rounded because 1140 g / 1000 is exact but plenty
        # of values are not, and a sensor state of 1.1400000000000001 is not.
        "weight": round(weight / 1000, 3) if isinstance(weight, (int, float)) else None,
        "dimensions": dimensions,
        "history": build_history(raw.get("events")) if include_history else None,
        "raw": raw,
    }


def sort_parcels_by_ts(
    parcels: list[dict], key_field: str, *, descending: bool = False
) -> list[dict]:
    """Return normalised parcels sorted by the ISO timestamp at ``key_field``.

    The suite's sort contract: incoming/outgoing ascending on ``planned_from``,
    delivered descending on ``delivered_at``. Parcels whose value is missing or
    unparseable always sort to the end, regardless of ``descending``.
    """
    with_ts: list[tuple[datetime, dict]] = []
    without_ts: list[dict] = []
    for parcel in parcels:
        parsed = parse_iso(parcel.get(key_field))
        if parsed is None:
            without_ts.append(parcel)
        else:
            with_ts.append((parsed, parcel))
    with_ts.sort(key=lambda item: item[0], reverse=descending)
    return [parcel for _, parcel in with_ts] + without_ts


def apply_delivered_filter(parcels: list[dict], entry: ConfigEntry) -> list[dict]:
    """Trim the delivered list per the entry's retention option.

    ``parcels`` must already be sorted newest-first. ``days`` keeps deliveries
    from the last N days (an unparseable ``delivered_at`` is kept rather than
    silently dropped); the ``parcels`` type keeps the N most recent. Parcels
    stay *tracked* either way — this only controls what the delivered sensor
    shows.
    """
    options = entry.options
    filter_type = options.get(
        CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
    )
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(timezone.utc) - timedelta(days=amount)
        return [
            parcel
            for parcel in parcels
            if (parsed := parse_iso(parcel.get("delivered_at"))) is None
            or parsed >= cutoff
        ]
    return parcels[:amount]
