"""Services for the Swiss Post parcel tracker integration.

`swiss_post.track_parcel` / `swiss_post.untrack_parcel` let you add or remove a
tracked parcel without opening the integration options — so a Lovelace button
can start tracking a parcel straight from a dashboard.
"""
from __future__ import annotations

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .config_flow import normalize_tracking_code, valid_tracking_code
from .const import (
    CONF_PARCELS,
    CONF_SOURCE,
    CONF_TRACKING_CODE,
    DOMAIN,
    SOURCE_TRACKING,
)

SERVICE_TRACK_PARCEL = "track_parcel"
SERVICE_UNTRACK_PARCEL = "untrack_parcel"

_TRACK_SCHEMA = vol.Schema({vol.Required(CONF_TRACKING_CODE): cv.string})
_UNTRACK_SCHEMA = vol.Schema({vol.Required(CONF_TRACKING_CODE): cv.string})


def _resolve_entry(hass: HomeAssistant):
    """Return the tracking-code hub, or raise when there is none.

    Must filter on the source: these services write tracking codes into
    ``CONF_PARCELS``, which only the tracking coordinator reads. An account hub
    discovers its own parcels, so writing a code into *its* options would be
    silently ignored — and since an account entry can be created first, picking
    "the first entry" is not good enough.
    """
    entries = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.data.get(CONF_SOURCE, SOURCE_TRACKING) == SOURCE_TRACKING
    ]
    if not entries:
        raise ServiceValidationError(
            "Swiss Post tracking-code hub is not set up — these services add "
            "codes by hand, which a SwissID account hub does not use"
        )
    return entries[0]


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the Swiss Post services (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_TRACK_PARCEL):
        return

    async def _track(call: ServiceCall) -> None:
        tracking_code = normalize_tracking_code(call.data[CONF_TRACKING_CODE])
        if not valid_tracking_code(tracking_code):
            raise ServiceValidationError(
                f"'{tracking_code}' is not a valid Swiss Post tracking code"
            )
        entry = _resolve_entry(hass)

        parcels = [dict(p) for p in entry.options.get(CONF_PARCELS, [])]
        if any(p[CONF_TRACKING_CODE] == tracking_code for p in parcels):
            return  # already tracked — no-op
        parcels.append({CONF_TRACKING_CODE: tracking_code})
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_PARCELS: parcels}
        )

    async def _untrack(call: ServiceCall) -> None:
        tracking_code = normalize_tracking_code(call.data[CONF_TRACKING_CODE])
        entry = _resolve_entry(hass)
        current = entry.options.get(CONF_PARCELS, [])
        kept = [p for p in current if p[CONF_TRACKING_CODE] != tracking_code]
        if len(kept) != len(current):
            hass.config_entries.async_update_entry(
                entry, options={**entry.options, CONF_PARCELS: kept}
            )

    hass.services.async_register(
        DOMAIN, SERVICE_TRACK_PARCEL, _track, schema=_TRACK_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_UNTRACK_PARCEL, _untrack, schema=_UNTRACK_SCHEMA
    )


def async_unload_services(hass: HomeAssistant) -> None:
    """Remove the Swiss Post services (single-entry integration, so on unload)."""
    for service in (SERVICE_TRACK_PARCEL, SERVICE_UNTRACK_PARCEL):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)
