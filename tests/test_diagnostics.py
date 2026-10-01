"""Tests for Swiss Post diagnostics."""
from unittest.mock import MagicMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

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
    entry.runtime_data.coordinator.delivered_codes = set()

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["counts"] == {
        "incoming_active": 1,
        "delivered": 0,
        "outgoing_active": 0,
        "outgoing_delivered": 0,
        "skipped_from_fetch": 0,
    }
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


async def test_diagnostics_reports_the_account_four_way_split(hass):
    """The account coordinator publishes a dict; diagnostics must read it."""
    from custom_components.swiss_post.const import (
        CONF_ID_TOKEN,
        CONF_REFRESH_TOKEN,
        CONF_SOURCE,
        DOMAIN,
        SOURCE_ACCOUNT,
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="account:user-1",
        data={
            CONF_SOURCE: SOURCE_ACCOUNT,
            CONF_ID_TOKEN: "secret-idt",
            CONF_REFRESH_TOKEN: "secret-rt",
            "device_id": "dev-1",
            "email": "me@example.ch",
        },
        options={},
    )
    entry.add_to_hass(hass)
    entry.runtime_data = MagicMock()
    coordinator = entry.runtime_data.coordinator
    coordinator.data = {
        "incoming_active": [{"barcode": "A", "raw": {"mailpieceId": "A"}}],
        "incoming_delivered": [],
        "outgoing_active": [{"barcode": "B", "raw": {}}],
        "outgoing_delivered": [{"barcode": "C", "raw": {}}],
    }
    coordinator.delivered_codes = set()

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["counts"] == {
        "incoming_active": 1,
        "delivered": 0,
        "outgoing_active": 1,
        "outgoing_delivered": 1,
        "skipped_from_fetch": 0,
    }
    assert len(result["outgoing"]) == 1
    assert len(result["delivered_outgoing"]) == 1
    # The OAuth tokens and the account identity must never reach an issue.
    assert result["entry_data"][CONF_ID_TOKEN] == "**REDACTED**"
    assert result["entry_data"][CONF_REFRESH_TOKEN] == "**REDACTED**"
    assert result["entry_data"]["device_id"] == "**REDACTED**"
    assert result["entry_data"]["email"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["mailpieceId"] == "**REDACTED**"
