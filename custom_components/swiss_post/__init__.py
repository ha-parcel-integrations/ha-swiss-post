"""Swiss Post parcel tracker custom component for Home Assistant."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import (
    async_create_clientsession,
    async_get_clientsession,
)

from .account.client import SwissPostAccountClient
from .account.coordinator import SwissPostAccountCoordinator
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_DEVICE_ID,
    CONF_ID_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_SOURCE,
    DOMAIN,
    PLATFORMS,
    SOURCE_ACCOUNT,
    SOURCE_TRACKING,
)
from .services import async_setup_services, async_unload_services
from .tracking.api import SwissPostApiClient
from .tracking.coordinator import SwissPostCoordinator

_LOGGER = logging.getLogger(__name__)


@dataclass
class SwissPostData:
    """Runtime data attached to the Swiss Post config entry."""

    client: SwissPostApiClient | SwissPostAccountClient
    coordinator: SwissPostCoordinator | SwissPostAccountCoordinator


type SwissPostConfigEntry = ConfigEntry[SwissPostData]


def _is_account(entry: ConfigEntry) -> bool:
    """Whether this entry is an account inbox (vs a public tracking hub)."""
    return entry.data.get(CONF_SOURCE) == SOURCE_ACCOUNT


async def async_setup_entry(hass: HomeAssistant, entry: SwissPostConfigEntry) -> bool:
    """Set up Swiss Post from a config entry."""
    if _is_account(entry):
        async def async_store_tokens(tokens: dict[str, str]) -> None:
            """Persist the rotated token pair after every refresh."""
            hass.config_entries.async_update_entry(
                entry,
                data={
                    **entry.data,
                    CONF_ID_TOKEN: tokens["id_token"],
                    CONF_REFRESH_TOKEN: tokens["refresh_token"],
                    CONF_ACCESS_TOKEN: tokens.get("access_token", ""),
                },
            )

        client: SwissPostApiClient | SwissPostAccountClient = SwissPostAccountClient(
            async_get_clientsession(hass),
            id_token=entry.data.get(CONF_ID_TOKEN),
            refresh_token=entry.data.get(CONF_REFRESH_TOKEN),
            device_id=entry.data.get(CONF_DEVICE_ID),
            token_callback=async_store_tokens,
        )
        coordinator: SwissPostCoordinator | SwissPostAccountCoordinator = (
            SwissPostAccountCoordinator(hass, client, entry)
        )
    else:
        # Swiss Post tracking is public — no credentials — but the ekp-web
        # surface hands out an anonymous session cookie that every lookup is
        # authorised with. That needs a *dedicated* client session: on HA's
        # shared one the cookie jar is shared too, and the Swiss Post cookie
        # would ride along on every other integration's requests.
        # ``async_create_clientsession`` closes it again when this entry unloads.
        client = SwissPostApiClient(async_create_clientsession(hass))
        coordinator = SwissPostCoordinator(hass, client, entry)

    # Fetch initial data here, before forwarding to platforms. Raising
    # ConfigEntryNotReady from a forwarded platform is too late for HA to catch
    # cleanly; doing the first refresh here lets a transient failure fail the
    # whole entry so HA retries it with backoff. A dead refresh chain surfaces
    # as ConfigEntryAuthFailed from the account coordinator, which HA turns into
    # the reauth flow on its own.
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = SwissPostData(client=client, coordinator=coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Apply option changes (added/removed parcels, history) live via a
    # coordinator refresh — no reload — so per-parcel sensors appear and
    # disappear immediately.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    # The track_parcel service only makes sense for a tracking hub; the account
    # inbox discovers its own parcels.
    if not _is_account(entry):
        async_setup_services(hass)

    return True


async def _async_options_updated(
    hass: HomeAssistant, entry: SwissPostConfigEntry
) -> None:
    """Apply changed options by refreshing the coordinator."""
    await entry.runtime_data.coordinator.async_request_refresh()


async def async_unload_entry(hass: HomeAssistant, entry: SwissPostConfigEntry) -> bool:
    """Unload the Swiss Post config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    # The services are shared across tracking hubs, so only remove them once the
    # last tracking hub is gone — otherwise unloading one would break the others.
    others_loaded = any(
        other.entry_id != entry.entry_id
        and other.state is ConfigEntryState.LOADED
        and other.data.get(CONF_SOURCE, SOURCE_TRACKING) == SOURCE_TRACKING
        for other in hass.config_entries.async_entries(DOMAIN)
    )
    if not _is_account(entry) and not others_loaded:
        async_unload_services(hass)
    return True
