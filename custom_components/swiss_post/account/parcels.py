"""Canonical mapping for the SwissID account inbox (mobserv overview).

Maps a ``MailpieceTrackingOverviewElement`` — the per-parcel summary the inbox
returns — onto the suite's canonical parcel shape. Field names and the status
vocabulary are taken from the app's own ``kotlinx.serialization`` models, so
this is evidence-based rather than guessed.

The overview is a **summary**: it carries status, delivery address, pickup
office and direction, but no weight, dimensions, ETA window or event timeline —
those live on the per-parcel ``detail`` call and are a later enrichment. The
fields this cannot fill are ``None``, never omitted, same as every other
carrier.
"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

from ..const import ACCOUNT_TRACKING_URL, ParcelStatus
from ..parcels import NEW_ISSUE_URL, epoch_ms_to_iso, warn_once

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


def normalize_account_parcel(raw: dict[str, Any]) -> dict[str, Any]:
    """Return the canonical parcel shape for one overview element.

    The returned dict's keys are the suite-wide contract; keys the overview
    summary does not carry (weight, dimensions, delivery window, history) are
    ``None``.
    """
    barcode = raw.get("mailpieceId")
    raw_status = raw.get("mailpieceStatusType")
    status = map_account_status(str(raw_status) if raw_status is not None else None)
    delivered = bool(raw.get("isComplete")) or status is ParcelStatus.DELIVERED
    pickup = not delivered and status is ParcelStatus.AT_PICKUP_POINT

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
        # The overview summary carries no ETA window.
        "planned_from": None,
        "planned_to": None,
        "pickup": pickup,
        "pickup_point": _pickup_point(raw) if pickup else None,
        "url": (
            ACCOUNT_TRACKING_URL.format(tracking_code=quote(str(barcode), safe=""))
            if barcode
            else None
        ),
        # Filled by a future per-parcel detail enrichment, not by the summary.
        "weight": None,
        "dimensions": None,
        "history": None,
        "raw": raw,
    }
