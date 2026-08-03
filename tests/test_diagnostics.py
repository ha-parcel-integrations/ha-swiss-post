"""Tests for Swiss Post diagnostics."""
from unittest.mock import MagicMock

from custom_components.swiss_post.diagnostics import (
    async_get_config_entry_diagnostics,
)


async def test_diagnostics_redacts_and_counts(hass):
    """Diagnostics get pasted into public issues — nothing identifying may survive."""
    entry = MagicMock()
    entry.options = {"parcels": [{"tracking_code": "990012345678901234"}]}
    entry.runtime_data.coordinator.data = [
        {
            "barcode": "990012345678901234",
            "sender": None,
            "receiver": "3000 Bern",
            "status": "out_for_delivery",
            "raw": {
                "shipmentNumber": "990012345678901234",
                "identity": "a-per-shipment identifier",
                "addressee": {"zip": "3000", "city": "Bern"},
                "deliveryPostOfficeZip": "3011",
                "frankingLicense": "the sender's billing licence",
                "globalStatus": "IN_DELIVERY",
            },
        }
    ]
    entry.runtime_data.coordinator.delivered = []

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["counts"] == {"incoming_active": 1, "delivered": 0}
    # tracking codes and payload PII are redacted, at every nesting level
    assert result["entry_options"]["parcels"][0]["tracking_code"] == "**REDACTED**"
    assert result["incoming"][0]["barcode"] == "**REDACTED**"
    assert result["incoming"][0]["receiver"] == "**REDACTED**"
    raw = result["incoming"][0]["raw"]
    assert raw["shipmentNumber"] == "**REDACTED**"
    assert raw["identity"] == "**REDACTED**"
    assert raw["addressee"] == "**REDACTED**"
    assert raw["deliveryPostOfficeZip"] == "**REDACTED**"
    assert raw["frankingLicense"] == "**REDACTED**"
    # non-identifying fields survive, or the diagnostics would be useless
    assert result["incoming"][0]["status"] == "out_for_delivery"
    assert raw["globalStatus"] == "IN_DELIVERY"
