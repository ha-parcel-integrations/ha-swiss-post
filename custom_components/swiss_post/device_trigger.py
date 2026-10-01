"""Device triggers for the Swiss Post parcel tracker integration.

Surfaces the parcel events the coordinator fires on the HA event bus as
no-code automation triggers. Each trigger filters on the hub's ``device_id``
(attached to every event).
"""
from __future__ import annotations

import voluptuous as vol
from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA
from homeassistant.components.homeassistant.triggers import event as event_trigger
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_DOMAIN,
    CONF_PLATFORM,
    CONF_TYPE,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
from homeassistant.helpers.typing import ConfigType

from .const import CONF_SOURCE, DOMAIN, SOURCE_ACCOUNT, SOURCE_TRACKING

# Device-trigger type -> bus event fired by the coordinator.
TRIGGER_EVENTS = {
    "parcel_registered": f"{DOMAIN}_parcel_registered",
    "parcel_status_changed": f"{DOMAIN}_parcel_status_changed",
    "parcel_delivered": f"{DOMAIN}_parcel_delivered",
    "parcel_delivery_time_changed": f"{DOMAIN}_parcel_delivery_time_changed",
    "outgoing_parcel_status_changed": f"{DOMAIN}_outgoing_parcel_status_changed",
    "outgoing_parcel_delivered": f"{DOMAIN}_outgoing_parcel_delivered",
}
TRIGGER_TYPES = set(TRIGGER_EVENTS)
# Fired only by the account coordinator, which is the only source that knows
# which parcels the user sent.
OUTGOING_TRIGGER_TYPES = {
    "outgoing_parcel_status_changed",
    "outgoing_parcel_delivered",
}

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend(
    {vol.Required(CONF_TYPE): vol.In(TRIGGER_TYPES)}
)


async def async_get_triggers(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, str]]:
    """Return the list of parcel triggers for a Swiss Post device.

    The sender-side triggers are only offered on an account hub: a tracking-code
    hub has no notion of a parcel you sent, so they could never fire there and
    would just be dead entries in the automation picker.
    """
    trigger_types = set(TRIGGER_TYPES)
    if not _is_account_device(hass, device_id):
        trigger_types -= OUTGOING_TRIGGER_TYPES
    return [
        {
            CONF_PLATFORM: "device",
            CONF_DOMAIN: DOMAIN,
            CONF_DEVICE_ID: device_id,
            CONF_TYPE: trigger_type,
        }
        for trigger_type in sorted(trigger_types)
    ]


def _is_account_device(hass: HomeAssistant, device_id: str) -> bool:
    """Whether ``device_id`` belongs to a SwissID account hub."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return False
    return any(
        (entry := hass.config_entries.async_get_entry(entry_id)) is not None
        and entry.data.get(CONF_SOURCE, SOURCE_TRACKING) == SOURCE_ACCOUNT
        for entry_id in device.config_entries
    )


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Attach a device trigger by delegating to the event trigger."""
    event_config = event_trigger.TRIGGER_SCHEMA(
        {
            event_trigger.CONF_PLATFORM: "event",
            event_trigger.CONF_EVENT_TYPE: TRIGGER_EVENTS[config[CONF_TYPE]],
            event_trigger.CONF_EVENT_DATA: {CONF_DEVICE_ID: config[CONF_DEVICE_ID]},
        }
    )
    return await event_trigger.async_attach_trigger(
        hass, event_config, action, trigger_info, platform_type="device"
    )
