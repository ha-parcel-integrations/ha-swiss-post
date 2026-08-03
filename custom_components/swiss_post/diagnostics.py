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
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SwissPostConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for the Swiss Post config entry."""
    coordinator = entry.runtime_data.coordinator

    return {
        "entry_options": async_redact_data(dict(entry.options), TO_REDACT),
        "counts": {
            "incoming_active": len(coordinator.data or []),
            "delivered": len(coordinator.delivered or []),
        },
        "incoming": async_redact_data(coordinator.data or [], TO_REDACT),
        "delivered": async_redact_data(coordinator.delivered or [], TO_REDACT),
    }
