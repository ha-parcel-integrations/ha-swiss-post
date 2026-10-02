"""Canonical mapping for the SwissID account inbox (mobserv).

Maps a ``MailpieceTrackingOverviewElement`` — the per-parcel summary the inbox
returns — onto the suite's canonical parcel shape. Field names and the status
vocabulary are taken from the app's own ``kotlinx.serialization`` models, so
this is evidence-based rather than guessed.

The overview is a **summary**: it carries status, delivery address, pickup
office and direction, but no weight, dimensions or event timeline. Those come
from the optional per-parcel enrichment record, which
:func:`normalize_account_parcel` takes as ``detail`` and merges in. Without it
those fields stay ``None`` — never omitted, same as every other carrier.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any
from urllib.parse import quote

from ..const import ACCOUNT_TRACKING_URL, HISTORY_MAX_EVENTS, ParcelStatus
from ..parcels import (
    NEW_ISSUE_URL,
    epoch_ms_to_iso,
    format_dimensions,
    parse_iso,
    warn_once,
)

_LOGGER = logging.getLogger(__name__)

# ``mailpieceStatusType`` is the app's own enum (distinct from the public
# tracking surface's ``globalStatus``). Values confirmed from the APK's
# MailpieceStatusType model.
_STATUS_MAP: dict[str, ParcelStatus] = {
    "NOT_YET_SENT": ParcelStatus.REGISTERED,
    "ON_GOING_DELIVERY": ParcelStatus.IN_TRANSIT,
    "WAITING_FOR_PICKUP": ParcelStatus.AT_PICKUP_POINT,
    "DELIVERED": ParcelStatus.DELIVERED,
    "NOT_DELIVERED": ParcelStatus.PROBLEM,
    "RETURNED": ParcelStatus.RETURNING,
    "UNKNOWN": ParcelStatus.UNKNOWN,
}


def map_account_status(code: str | None) -> ParcelStatus:
    """Map a ``mailpieceStatusType`` to a canonical :class:`ParcelStatus`."""
    if not code:
        return ParcelStatus.UNKNOWN
    mapped = _STATUS_MAP.get(code)
    if mapped is not None:
        return mapped
    warn_once(
        f"account_status:{code}",
        _LOGGER,
        "Unrecognised Swiss Post account status — help us map it. Open an issue "
        "and paste this line: %s\n  mailpieceStatusType=%s → reported as 'unknown'",
        NEW_ISSUE_URL,
        code,
    )
    return ParcelStatus.UNKNOWN


def is_letter(raw: dict[str, Any]) -> bool:
    """Whether this inbox item is a letter rather than a parcel.

    Letters are excluded from this integration (no scan-preview feature exists
    on Swiss Post's side). An unknown ``mailpieceType`` is kept, so a parcel is
    never dropped for an unexpected type value.
    """
    return str(raw.get("mailpieceType", "")).upper() == "LETTER"


def is_outgoing(raw: dict[str, Any]) -> bool:
    """Whether the logged-in user is the sender of this item.

    ``userIsSender`` is the app's own flag. Anything falsy is treated as
    incoming, so an item is never hidden from both the incoming and outgoing
    views at once.
    """
    return bool(raw.get("userIsSender"))


def _receiver(raw: dict[str, Any]) -> str | None:
    """Return the recipient as "zip city".

    The ``name*`` fields are PII and come back blank on this surface (as on the
    public one), so the town is all there is — and all a dashboard wants.
    """
    address = raw.get("deliveryAddress") or {}
    detail = address.get("addressDetail") or {}
    parts = [
        str(part).strip()
        for part in (detail.get("zip4"), detail.get("city"))
        if part
    ]
    return " ".join(parts) or None


def _pickup_point(raw: dict[str, Any]) -> str | None:
    """Return the pickup office name, if any."""
    address = raw.get("deliveryAddress") or {}
    office = address.get("pickupOffice")
    return str(office) if office else None


# The enrichment record states weight and dimensions as **display strings**
# ("1.14 kg", "40.0 x 25.0 x 15.5 cm"), not numbers — the one place this backend
# is harder to consume than the public tracking surface. Whether they are
# localised under another Accept-Language has not been probed, so a decimal
# comma is accepted up front and anything that still will not parse is reported
# once rather than guessed at.
_NUMBER = r"\d+(?:[.,]\d+)?"
_WEIGHT_RE = re.compile(rf"^\s*({_NUMBER})\s*(kg|g)\s*$", re.IGNORECASE)
_DIMENSIONS_RE = re.compile(
    rf"^\s*({_NUMBER})\s*[x×]\s*({_NUMBER})\s*[x×]\s*({_NUMBER})\s*(cm|mm)\s*$",
    re.IGNORECASE,
)
_WEIGHT_TO_KG = {"kg": 1.0, "g": 0.001}
_LENGTH_TO_CM = {"cm": 1.0, "mm": 0.1}


def _decimal(text: str) -> float:
    """Parse one number that may use either a decimal point or a comma."""
    return float(text.replace(",", "."))


def _unparsed(field: str, value: str) -> None:
    """Report a physical-dimensions string we could not read, once.

    The *shape* of the value is logged, never the value: a weight is harmless
    but this keeps the rule one-sided and obvious.
    """
    warn_once(
        f"account_physical:{field}",
        _LOGGER,
        "Swiss Post stated a parcel's %s in a format we cannot read yet (%d "
        "characters), so it is reported as unknown. Please report it: %s",
        field,
        len(value),
        NEW_ISSUE_URL,
    )


def _weight_kg(detail: dict[str, Any]) -> float | None:
    """Return the parcel's weight in kilograms from the formatted string."""
    physical = detail.get("physicalDimensions") or {}
    value = physical.get("weight")
    if not isinstance(value, str) or not value.strip():
        return None
    match = _WEIGHT_RE.match(value)
    if match is None:
        _unparsed("weight", value)
        return None
    # Rounded so a sensor state is not 0.8500000000000001.
    return round(_decimal(match[1]) * _WEIGHT_TO_KG[match[2].lower()], 3)


def _dimensions(detail: dict[str, Any]) -> dict[str, Any] | None:
    """Return the canonical dimensions from the formatted string.

    The three numbers are sorted before they are labelled, exactly as on the
    public tracking surface: Swiss Post's own frontend sorts them too, so their
    order in the payload carries no axis meaning.
    """
    physical = detail.get("physicalDimensions") or {}
    value = physical.get("dimensions")
    if not isinstance(value, str) or not value.strip():
        return None
    match = _DIMENSIONS_RE.match(value)
    if match is None:
        _unparsed("dimensions", value)
        return None
    factor = _LENGTH_TO_CM[match[4].lower()]
    height, width, length = sorted(_decimal(match[index]) * factor for index in (1, 2, 3))
    return format_dimensions(length, width, height)


def build_account_history(
    events: list | None, *, max_events: int = HISTORY_MAX_EVENTS
) -> list[dict]:
    """Build the canonical ``history`` list from the enrichment ``events``.

    Each entry is ``{timestamp, status, raw_status}``, oldest → newest, capped
    to the most recent ``max_events``. ``eventType`` carried ``OTHER`` on every
    event of every parcel seen so far — including the delivery itself — so it is
    not a status vocabulary and every entry keeps ``status: null``, the same
    decision the public timeline made. The human-readable ``title`` becomes
    ``raw_status``.

    The per-event ``location`` coordinates are deliberately **not** copied here:
    they say where a parcel physically was, and an attribute is far more
    exposed than the raw payload.
    """
    parseable: list[tuple[datetime, dict]] = []
    unparseable: list[dict] = []
    unmapped_types: set[str] = set()
    for event in events or []:
        if not isinstance(event, dict):
            continue
        timestamp = epoch_ms_to_iso(event.get("timestamp"))
        if not timestamp:
            continue
        event_type = event.get("eventType")
        if isinstance(event_type, str) and event_type.upper() not in ("OTHER", ""):
            unmapped_types.add(event_type)
        entry = {
            "timestamp": timestamp,
            "status": None,
            "raw_status": event.get("title") or event.get("subtitle"),
        }
        parsed = parse_iso(timestamp)
        if parsed is None:
            unparseable.append(entry)
        else:
            parseable.append((parsed, entry))

    if unmapped_types:
        warn_once(
            "account_event_types",
            _LOGGER,
            "Swiss Post tagged a parcel event with an eventType other than "
            "'OTHER' (%s) — the first time we have seen one, so it may be a "
            "status vocabulary worth mapping. Please report it: %s",
            ", ".join(sorted(unmapped_types)),
            NEW_ISSUE_URL,
        )

    parseable.sort(key=lambda item: item[0])
    ordered = [entry for _, entry in parseable] + unparseable
    return ordered[-max_events:]


def normalize_account_parcel(
    raw: dict[str, Any], *, detail: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Return the canonical parcel shape for one overview element.

    The returned dict's keys are the suite-wide contract. ``detail`` is the
    optional per-parcel enrichment record; without it, the fields the inbox
    summary does not carry (weight, dimensions, history) stay ``None``.
    """
    barcode = raw.get("mailpieceId")
    raw_status = raw.get("mailpieceStatusType")
    status = map_account_status(str(raw_status) if raw_status is not None else None)
    # **Not** ``isComplete``: that means "this parcel is finished", and it is
    # true on a returned parcel as well — which would file a parcel that came
    # back to the sender as delivered. The status enum is the only field that
    # distinguishes the two.
    delivered = status is ParcelStatus.DELIVERED
    pickup = not delivered and status is ParcelStatus.AT_PICKUP_POINT

    # The only ETA candidate in the whole model, and it has never been seen
    # populated. Anything that fills it is worth a look before it is mapped to
    # planned_from/planned_to: on a pickup parcel it could just as easily be the
    # deadline to collect it as an expected delivery.
    if raw.get("statusEndTimestamp") and not raw.get("isComplete"):
        warn_once(
            "account_status_end",
            _LOGGER,
            "Swiss Post reported an end timestamp on a parcel still under way — "
            "the first time we have seen one, and the only delivery-window "
            "candidate the account inbox has. Please check what the app shows "
            "for this parcel and report it: %s",
            NEW_ISSUE_URL,
        )

    enrichment = detail or {}

    return {
        "carrier": "Swiss Post",
        "barcode": barcode,
        # The counterparty's name is not in the summary; direction is known
        # from is_outgoing(), the name is not.
        "sender": None,
        "receiver": _receiver(raw),
        "status": status,
        "raw_status": str(raw_status) if raw_status is not None else None,
        "delivered": delivered,
        "delivered_at": epoch_ms_to_iso(raw.get("statusTimestamp")) if delivered else None,
        # Neither the summary nor the enrichment record carries an ETA: a
        # delivered parcel showed no such field, and ``statusEndTimestamp``
        # above has never been populated.
        "planned_from": None,
        "planned_to": None,
        "pickup": pickup,
        "pickup_point": _pickup_point(raw) if pickup else None,
        "url": (
            ACCOUNT_TRACKING_URL.format(tracking_code=quote(str(barcode), safe=""))
            if barcode
            else None
        ),
        # From the enrichment record only; None when it was not fetched.
        "weight": _weight_kg(enrichment),
        "dimensions": _dimensions(enrichment),
        "history": build_account_history(enrichment.get("events")) if detail else None,
        "raw": {**raw, "detail": detail} if detail else raw,
    }
