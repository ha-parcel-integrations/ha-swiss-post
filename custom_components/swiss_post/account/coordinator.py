"""Coordinator for the SwissID account inbox source.

Unlike the public tracking coordinator (which polls each code and can suspend
entirely), the account inbox is one batched fetch and is polled at a steady
cadence. It publishes the same canonical parcel lists, split four ways
(incoming/outgoing × active/delivered), so the shared presentation layer can
read them through its ``_bucket`` helper.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from ..const import DOMAIN, MID_INTERVAL_MINUTES, ParcelStatus
from ..events import (
    fire_incoming_change_events,
    fire_outgoing_change_events,
    snapshot_delivery_times,
    snapshot_states,
)
from ..parcels import apply_delivered_filter, sort_parcels_by_ts
from .client import (
    SwissPostAccountApiError,
    SwissPostAccountClient,
    SwissPostAccountReauthRequired,
)
from .parcels import is_letter, is_outgoing, normalize_account_parcel

_LOGGER = logging.getLogger(__name__)


class SwissPostAccountCoordinator(DataUpdateCoordinator[dict[str, list[dict]]]):
    """Refresh the whole account inbox on a steady cadence."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: SwissPostAccountClient,
        entry: ConfigEntry,
    ) -> None:
        """Initialise a continuously-polled account inbox coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} account",
            update_interval=timedelta(minutes=MID_INTERVAL_MINUTES),
        )
        self._client = client
        # None on the first refresh deliberately suppresses historical events.
        self._known_state: dict[str, ParcelStatus] | None = None
        self._known_delivery_times: (
            dict[str, tuple[str | None, str | None]] | None
        ) = None
        self._known_outgoing_state: dict[str, ParcelStatus] | None = None
        self._cached_device_id: str | None = None
        self.last_success_time: datetime | None = None
        # Account polling never suspends and never skips an individual parcel,
        # so these two mirror the tracking coordinator's interface as constants.
        self._current_tier_minutes: int | None = MID_INTERVAL_MINUTES

    @property
    def current_tier_minutes(self) -> int | None:
        """Steady account cadence (diagnostics only)."""
        return self._current_tier_minutes

    @property
    def delivered_codes(self) -> set[str]:
        """Account polls are batched; no parcel is ever skipped from a fetch."""
        return set()

    def _device_id(self) -> str | None:
        """Resolve (and cache) this entry's device id for event payloads."""
        if self._cached_device_id is not None:
            return self._cached_device_id
        registry = dr.async_get(self.hass)
        device = next(
            iter(
                dr.async_entries_for_config_entry(registry, self.config_entry.entry_id)
            ),
            None,
        )
        if device is not None:
            self._cached_device_id = device.id
        return self._cached_device_id

    async def _async_update_data(self) -> dict[str, list[dict]]:
        """Fetch the inbox and split it four ways."""
        try:
            payload = await self._client.async_get_overview()
        except SwissPostAccountReauthRequired as err:
            # ConfigEntryAuthFailed is the only exception DataUpdateCoordinator
            # turns into HA's reauth flow. Anything else — including re-raising
            # our own error — is swallowed into ConfigEntryNotReady/UpdateFailed
            # and the entry would retry a dead refresh chain forever instead of
            # asking the user to sign in again.
            raise ConfigEntryAuthFailed(
                "Swiss Post account needs to be signed in again"
            ) from err
        except SwissPostAccountApiError as err:
            raise UpdateFailed("Unable to update Swiss Post account inbox") from err

        raw_items = [
            item
            for key in ("inProgress", "completed")
            for item in (payload.get(key) or [])
            if isinstance(item, dict) and not is_letter(item)
        ]

        incoming_raw = [item for item in raw_items if not is_outgoing(item)]
        outgoing_raw = [item for item in raw_items if is_outgoing(item)]
        incoming = [normalize_account_parcel(item) for item in incoming_raw]
        outgoing = [normalize_account_parcel(item) for item in outgoing_raw]

        device_id = self._device_id()

        incoming_active = sort_parcels_by_ts(
            [p for p in incoming if not p["delivered"]], "planned_from"
        )
        incoming_delivered = apply_delivered_filter(
            sort_parcels_by_ts(
                [p for p in incoming if p["delivered"]], "delivered_at", descending=True
            ),
            self.config_entry,
        )
        outgoing_active = sort_parcels_by_ts(
            [p for p in outgoing if not p["delivered"]], "planned_from"
        )
        outgoing_delivered = apply_delivered_filter(
            sort_parcels_by_ts(
                [p for p in outgoing if p["delivered"]], "delivered_at", descending=True
            ),
            self.config_entry,
        )

        # Incoming = active + delivered, combined so the transition to delivered
        # is visible in one diff — same shape the tracking coordinator diffs.
        seen_incoming = incoming_active + incoming_delivered
        fire_incoming_change_events(
            self.hass,
            seen_incoming,
            self._known_state,
            self._known_delivery_times,
            device_id,
        )
        self._known_state = snapshot_states(seen_incoming)
        self._known_delivery_times = snapshot_delivery_times(seen_incoming)

        seen_outgoing = outgoing_active + outgoing_delivered
        fire_outgoing_change_events(
            self.hass, seen_outgoing, self._known_outgoing_state, device_id
        )
        self._known_outgoing_state = snapshot_states(seen_outgoing)

        self.last_success_time = datetime.now(timezone.utc)
        return {
            "incoming_active": incoming_active,
            "incoming_delivered": incoming_delivered,
            "outgoing_active": outgoing_active,
            "outgoing_delivered": outgoing_delivered,
        }
