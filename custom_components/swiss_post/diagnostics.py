"""Diagnostics support for the Swiss Post parcel tracker integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import SwissPostConfigEntry

# Diagnostics are pasted into public issues, so redact anything that
# identifies a person, an address or a specific parcel. Over-redacting is
# cheap; under-redacting leaks a user's home address into a GitHub thread.
#
TO_REDACT = {
    # canonical fields we publish ourselves
    "tracking_code",
    "barcode",
    "sender",
    "receiver",
    "pickup_point",
    "url",
    # Swiss Post payload fields — identity and address
    "shipmentNumber",
    "formattedShipmentNumber",
    "identifier",
    "identity",
    "shipmentId",
    "addressee",
    "originalAddressee",
    "name1",
    "name2",
    "name3",
    "street",
    "streetAndNumber",
    "number",
    "zip",
    "city",
    "deliveryPostOfficeZip",
    "expectedDeliveryZip",
    "expectedDeliveryDistrict",
    "senderDeliveryOfficeZip",
    "houseKey",
    "signature",
    "proofOfDeliveryImageId",
    "imageReference",
    # Swiss Post payload fields — sender / billing identity
    "frankingLicense",
    "esrRefNo",
    "esrnumber",
    "debitorDescription",
    "account",
    "kdpId",
    # session material: leaking these lets a reader query the same session
    "userIdentifier",
    "csrfToken",
    "NPKlpipSession",
    # account source: OAuth tokens and the device id are credentials — leaking
    # any of them lets a reader act as the user's account.
    "access_token",
    "id_token",
    "refresh_token",
    "device_id",
    "account_sub",
    "email",
    "Authorization",
    # account payload (mobserv overview) — identity and address
    "mailpieceId",
    "mailpieceKey",
    "deliveryAddress",
    "houseNumber",
    "zip4",
    "collectionCode",
    "pickupOffice",
    "summaryDescription",
    # account payload (per-parcel enrichment) — every event carries the
    # coordinates of where the parcel physically was, and the last one is the
    # user's doorstep.
    "location",
    "latitude",
    "longitude",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SwissPostConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for the Swiss Post config entry."""
    coordinator = entry.runtime_data.coordinator
    data = coordinator.data or []
    if isinstance(data, dict):
        incoming_active = data.get("incoming_active", [])
        incoming_delivered = data.get("incoming_delivered", [])
        outgoing_active = data.get("outgoing_active", [])
        outgoing_delivered = data.get("outgoing_delivered", [])
    else:
        incoming_active = data
        incoming_delivered = getattr(coordinator, "delivered", [])
        outgoing_active = []
        outgoing_delivered = []

    return {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "entry_options": async_redact_data(dict(entry.options), TO_REDACT),
        "polling": {
            "current_tier_minutes": coordinator.current_tier_minutes,
            "update_interval_seconds": (
                coordinator.update_interval.total_seconds()
                if coordinator.update_interval
                else None
            ),
            "suspended": coordinator.update_interval is None,
        },
        "counts": {
            "incoming_active": len(incoming_active),
            "delivered": len(incoming_delivered),
            "outgoing_active": len(outgoing_active),
            "outgoing_delivered": len(outgoing_delivered),
            "skipped_from_fetch": len(coordinator.delivered_codes),
        },
        "incoming": async_redact_data(incoming_active, TO_REDACT),
        "delivered": async_redact_data(incoming_delivered, TO_REDACT),
        "outgoing": async_redact_data(outgoing_active, TO_REDACT),
        "delivered_outgoing": async_redact_data(outgoing_delivered, TO_REDACT),
    }
