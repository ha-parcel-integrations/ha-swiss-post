"""The device every entity of this integration belongs to.

One place, because sensors, the button and the calendar must all land on the
*same* device entry — and because the account-based variant only has to change
this file to name devices per account.
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo

from .const import CONF_EMAIL, CONF_SOURCE, DOMAIN, SOURCE_ACCOUNT

CONFIGURATION_URL = "https://service.post.ch/ekp-web/ui/"

ATTRIBUTION = "Data provided by Swiss Post"


def build_device_info(entry: ConfigEntry) -> DeviceInfo:
    """Return the DeviceInfo shared by every entity of this hub.

    An account hub is named after its email so a tracking hub and one or more
    account hubs stay distinguishable in the device list.
    """
    is_account = entry.data.get(CONF_SOURCE) == SOURCE_ACCOUNT
    email = entry.data.get(CONF_EMAIL)
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=f"Swiss Post ({email})" if is_account and email else "Swiss Post",
        manufacturer="Swiss Post",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url=CONFIGURATION_URL,
    )
