"""Tests for Swiss Post setup and unload."""
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.swiss_post.const import (
    CONF_PARCELS,
    CONF_TRACKING_CODE,
    DOMAIN,
)
from custom_components.swiss_post.tracking.api import SwissPostApiError

from .payloads import ACTIVE_CODE
from .payloads import active_sample as _sample

OTHER_CODE = "990022222222222222"


async def test_setup_and_unload(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        options={CONF_PARCELS: [{CONF_TRACKING_CODE: ACTIVE_CODE}]},
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.swiss_post.tracking.api.SwissPostApiClient.async_get_parcel",
        new=AsyncMock(return_value=_sample()),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED

    # The active parcel produced a per-parcel sensor and the summary sensor.
    incoming = hass.states.get("sensor.swiss_post_incoming_parcels")
    assert incoming is not None
    assert incoming.state == "1"

    # Services registered on setup...
    assert hass.services.has_service(DOMAIN, "track_parcel")

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED

    # ...and removed on unload (single-instance integration).
    assert not hass.services.has_service(DOMAIN, "track_parcel")


async def test_setup_retries_when_first_refresh_fails(hass):
    """When the first data fetch fails, setup retries from the entry itself.

    The first refresh runs in __init__.py before platforms are forwarded, so a
    failure raises ConfigEntryNotReady from the entry setup (SETUP_RETRY) rather
    than — too late — from a forwarded platform.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        options={CONF_PARCELS: [{CONF_TRACKING_CODE: ACTIVE_CODE}]},
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.swiss_post.tracking.api.SwissPostApiClient.async_get_parcel",
        new=AsyncMock(side_effect=SwissPostApiError("Swiss Post unreachable")),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_per_parcel_sensor_spawn_and_remove(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        options={CONF_PARCELS: [{CONF_TRACKING_CODE: ACTIVE_CODE}]},
    )
    entry.add_to_hass(hass)

    mock = AsyncMock(return_value=_sample())
    with patch("custom_components.swiss_post.tracking.api.SwissPostApiClient.async_get_parcel", new=mock):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        registry = er.async_get(hass)
        assert registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_{ACTIVE_CODE}"
        )

        # The next poll returns a different tracking code: the summary sensor
        # spawns a new per-parcel sensor and removes the stale one.
        mock.return_value = _sample(OTHER_CODE)
        await entry.runtime_data.coordinator.async_request_refresh()
        await hass.async_block_till_done()

        assert registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_{OTHER_CODE}"
        )
        assert (
            registry.async_get_entity_id(
                "sensor", DOMAIN, f"{entry.entry_id}_{ACTIVE_CODE}"
            )
            is None
        )


async def test_options_update_applies_live_without_reload(hass):
    """Adding a parcel via options refreshes the coordinator immediately."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        options={CONF_PARCELS: [{CONF_TRACKING_CODE: ACTIVE_CODE}]},
    )
    entry.add_to_hass(hass)

    mock = AsyncMock(return_value=_sample())
    with patch("custom_components.swiss_post.tracking.api.SwissPostApiClient.async_get_parcel", new=mock):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        mock.side_effect = lambda code, **kwargs: _sample(code)
        hass.config_entries.async_update_entry(
            entry,
            options={
                **entry.options,
                CONF_PARCELS: [
                    {CONF_TRACKING_CODE: ACTIVE_CODE},
                    {CONF_TRACKING_CODE: OTHER_CODE},
                ],
            },
        )
        await hass.async_block_till_done()

    incoming = hass.states.get("sensor.swiss_post_incoming_parcels")
    assert incoming.state == "2"


# ---------------------------------------------------------------------------
# Account source setup
# ---------------------------------------------------------------------------


def _account_entry() -> MockConfigEntry:
    from custom_components.swiss_post.const import (
        CONF_DEVICE_ID,
        CONF_ID_TOKEN,
        CONF_REFRESH_TOKEN,
        CONF_SOURCE,
        SOURCE_ACCOUNT,
    )

    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="account:user-1",
        data={
            CONF_SOURCE: SOURCE_ACCOUNT,
            CONF_ID_TOKEN: "idt",
            CONF_REFRESH_TOKEN: "rt",
            CONF_DEVICE_ID: "dev-1",
            "email": "me@example.ch",
        },
        options={},
    )


async def test_account_entry_sets_up_and_skips_the_tracking_services(hass):
    """An account hub discovers its own parcels, so it registers no services."""
    entry = _account_entry()
    entry.add_to_hass(hass)

    overview = {"inProgress": [], "completed": [], "standaloneTaxCards": []}
    with patch(
        "custom_components.swiss_post.account.client.SwissPostAccountClient"
        ".async_get_overview",
        new=AsyncMock(return_value=overview),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    # The sender-side sensors only exist on an account hub.
    assert hass.states.get("sensor.swiss_post_me_example_ch_outgoing_parcels")
    # track_parcel belongs to the tracking hub, which this is not.
    assert not hass.services.has_service(DOMAIN, "track_parcel")

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_account_entry_persists_rotated_tokens(hass):
    """The refresh token rotates on every refresh — losing it breaks the chain."""
    from custom_components.swiss_post.const import CONF_ID_TOKEN, CONF_REFRESH_TOKEN

    entry = _account_entry()
    entry.add_to_hass(hass)

    overview = {"inProgress": [], "completed": [], "standaloneTaxCards": []}

    async def _fake_overview(self):
        # Simulate the client rotating and handing the new pair back.
        await self._token_callback(
            {"id_token": "idt-2", "refresh_token": "rt-2", "access_token": "at-2"}
        )
        return overview

    with patch(
        "custom_components.swiss_post.account.client.SwissPostAccountClient"
        ".async_get_overview",
        new=_fake_overview,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.data[CONF_ID_TOKEN] == "idt-2"
    assert entry.data[CONF_REFRESH_TOKEN] == "rt-2"


async def test_account_entry_starts_reauth_when_the_chain_is_dead(hass):
    """A dead refresh chain must ask the user to sign in, not retry forever."""
    from custom_components.swiss_post.account.client import (
        SwissPostAccountReauthRequired,
    )

    entry = _account_entry()
    entry.add_to_hass(hass)

    with patch(
        "custom_components.swiss_post.account.client.SwissPostAccountClient"
        ".async_get_overview",
        new=AsyncMock(side_effect=SwissPostAccountReauthRequired("dead")),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert [f for f in hass.config_entries.flow.async_progress() if f["context"]["source"] == "reauth"]
