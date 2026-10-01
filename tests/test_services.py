"""Tests for the Swiss Post services (track_parcel / untrack_parcel)."""
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.swiss_post.const import (
    CONF_PARCELS,
    CONF_TRACKING_CODE,
    DOMAIN,
)

from .payloads import active_sample

_SAMPLE = active_sample()



async def _setup(hass, parcels: list[dict] | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        options={CONF_PARCELS: parcels or []},
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.swiss_post.tracking.api.SwissPostApiClient.async_get_parcel",
        new=AsyncMock(return_value=_SAMPLE),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_track_parcel_adds_to_options(hass):
    entry = await _setup(hass)
    with patch(
        "custom_components.swiss_post.tracking.api.SwissPostApiClient.async_get_parcel",
        new=AsyncMock(return_value=_SAMPLE),
    ):
        await hass.services.async_call(
            DOMAIN,
            "track_parcel",
            {CONF_TRACKING_CODE: "990012345678901234"},
            blocking=True,
        )
        await hass.async_block_till_done()

    parcels = entry.options[CONF_PARCELS]
    assert parcels == [{CONF_TRACKING_CODE: "990012345678901234"}]


async def test_track_parcel_normalizes_code(hass):
    entry = await _setup(hass)
    with patch(
        "custom_components.swiss_post.tracking.api.SwissPostApiClient.async_get_parcel",
        new=AsyncMock(return_value=_SAMPLE),
    ):
        await hass.services.async_call(
            DOMAIN,
            "track_parcel",
            {CONF_TRACKING_CODE: "99.00 1234.5678 9012 34"},
            blocking=True,
        )
        await hass.async_block_till_done()

    assert entry.options[CONF_PARCELS] == [
        {CONF_TRACKING_CODE: "990012345678901234"}
    ]


async def test_track_parcel_rejects_empty_code(hass):
    await _setup(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "track_parcel", {CONF_TRACKING_CODE: ""}, blocking=True
        )


async def test_track_parcel_duplicate_is_noop(hass):
    entry = await _setup(hass)
    with patch(
        "custom_components.swiss_post.tracking.api.SwissPostApiClient.async_get_parcel",
        new=AsyncMock(return_value=_SAMPLE),
    ):
        for _ in range(2):
            await hass.services.async_call(
                DOMAIN,
                "track_parcel",
                {CONF_TRACKING_CODE: "990012345678901234"},
                blocking=True,
            )
            await hass.async_block_till_done()

    assert len(entry.options[CONF_PARCELS]) == 1


async def test_untrack_parcel_removes_from_options(hass):
    entry = await _setup(
        hass, parcels=[{CONF_TRACKING_CODE: "990012345678901234"}]
    )
    with patch(
        "custom_components.swiss_post.tracking.api.SwissPostApiClient.async_get_parcel",
        new=AsyncMock(return_value=_SAMPLE),
    ):
        await hass.services.async_call(
            DOMAIN,
            "untrack_parcel",
            {CONF_TRACKING_CODE: "990012345678901234"},
            blocking=True,
        )
        await hass.async_block_till_done()

    assert entry.options[CONF_PARCELS] == []


async def test_untrack_unknown_code_is_noop(hass):
    entry = await _setup(
        hass, parcels=[{CONF_TRACKING_CODE: "990012345678901234"}]
    )
    with patch(
        "custom_components.swiss_post.tracking.api.SwissPostApiClient.async_get_parcel",
        new=AsyncMock(return_value=_SAMPLE),
    ):
        await hass.services.async_call(
            DOMAIN,
            "untrack_parcel",
            {CONF_TRACKING_CODE: "nonsense"},
            blocking=True,
        )
        await hass.async_block_till_done()

    assert len(entry.options[CONF_PARCELS]) == 1


async def test_services_target_the_tracking_hub_not_the_account_hub(hass):
    """Codes added by service must land where a coordinator will read them.

    ``CONF_PARCELS`` is only read by the tracking coordinator. An account entry
    can be created first, so resolving "the first entry" would write the code
    into the account hub's options where nothing reads it — the parcel would
    silently never appear.
    """
    from custom_components.swiss_post.const import (
        CONF_SOURCE,
        SOURCE_ACCOUNT,
        SOURCE_TRACKING,
    )
    from custom_components.swiss_post.services import _resolve_entry

    account = MockConfigEntry(
        domain=DOMAIN,
        unique_id="account:user-1",
        data={CONF_SOURCE: SOURCE_ACCOUNT},
        options={},
    )
    account.add_to_hass(hass)
    tracking = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={CONF_SOURCE: SOURCE_TRACKING},
        options={CONF_PARCELS: []},
    )
    tracking.add_to_hass(hass)

    assert _resolve_entry(hass).entry_id == tracking.entry_id


async def test_services_reject_an_account_only_install(hass):
    """With no tracking hub there is nowhere to put a hand-entered code."""
    from homeassistant.exceptions import ServiceValidationError

    from custom_components.swiss_post.const import CONF_SOURCE, SOURCE_ACCOUNT
    from custom_components.swiss_post.services import _resolve_entry

    MockConfigEntry(
        domain=DOMAIN,
        unique_id="account:user-1",
        data={CONF_SOURCE: SOURCE_ACCOUNT},
        options={},
    ).add_to_hass(hass)

    with pytest.raises(ServiceValidationError):
        _resolve_entry(hass)
