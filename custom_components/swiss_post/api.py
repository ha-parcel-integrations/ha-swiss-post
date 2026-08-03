"""Swiss Post public tracking API client.

Two keyless hosts, each holding half the data (see :mod:`.const`):

* **surface A** — ``service.post.ch/ekp-web`` — status, ETA, weight,
  dimensions, delivery booleans. Reached through an anonymous session.
* **surface B** — ``eosapi.postlogistics.ch`` — the event timeline, fetched
  only when the history option is on, and merged into surface A's (always
  empty) ``events`` array so the normalised parcel has one place to look.

The contract the coordinator relies on:

* ``async_get_parcel`` returns the raw per-parcel dict on success,
* returns ``None`` when Swiss Post has nothing for the tracking code — an
  unknown number, or one that has not been scanned yet (a normal, expected
  state, never an error),
* raises :class:`SwissPostApiError` for anything else,
* lets ``aiohttp.ClientError`` propagate untouched — ``DataUpdateCoordinator``
  already wraps those into ``UpdateFailed``.
"""
from __future__ import annotations

import logging
from typing import Any

import aiohttp

from .const import (
    BROWSER_USER_AGENT,
    CSRF_HEADER,
    EKP_HISTORY_ITEM_URL,
    EKP_HISTORY_URL,
    EKP_REFERER,
    EKP_USER_URL,
    EOS_CULTURE,
    EOS_HISTORY_URL,
    EOS_ORIGIN,
)

_LOGGER = logging.getLogger(__name__)

_EKP_HEADERS = {
    "User-Agent": BROWSER_USER_AGENT,
    "Referer": EKP_REFERER,
}

_EOS_HEADERS = {
    "User-Agent": BROWSER_USER_AGENT,
    "Origin": EOS_ORIGIN,
    "Referer": f"{EOS_ORIGIN}/",
}

# Tracking codes we have already reported as empty, or as a malformed history
# request, so each is logged once per HA session instead of on every poll.
_empty_result_logged: set[str] = set()
_history_204_logged: set[str] = set()


class SwissPostApiError(Exception):
    """Raised when a Swiss Post API call returns an unexpected response."""

    def __init__(self, detail: str) -> None:
        """Store the detail that triggered the error."""
        super().__init__(f"Swiss Post API request failed: {detail}")
        self.detail = detail


class SwissPostSessionExpired(Exception):
    """Internal signal that the anonymous session was rejected (HTTP 403)."""


class SwissPostSession:
    """The anonymous ekp-web session — cookie jar, user id and CSRF token.

    Swiss Post's consumer API needs no credentials but does need a session:
    every lookup is authorised by a ``NPKlpipSession`` cookie plus the CSRF
    token and anonymous user id handed out by ``/api/user``. One session serves
    every parcel and survives across polls; a 403 means it died and is
    re-established transparently.

    The lookup itself is two calls:

    1. ``POST /history`` — registers the tracking number *in this session* and
       returns a hash,
    2. ``GET /history/not-included/{hash}`` — reads back what the session
       resolved.

    **Step 1 is not optional.** The hash is just ``sha256(tracking_number)``,
    so it looks cacheable — but without the POST, step 2 answers ``200`` with
    an empty list, which is indistinguishable from an unknown parcel. Skipping
    it would silently report every parcel as missing, forever, with no error.
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialise the helper with a cookie-carrying aiohttp session."""
        self._session = session
        self._user_identifier: str | None = None
        self._csrf_token: str | None = None

    @property
    def established(self) -> bool:
        """Whether a handshake has completed and has not since been invalidated."""
        return self._user_identifier is not None

    def invalidate(self) -> None:
        """Forget the current session so the next lookup re-handshakes."""
        self._user_identifier = None
        self._csrf_token = None

    async def async_establish(self) -> None:
        """Run the anonymous handshake, keeping the user id, token and cookie."""
        async with self._session.get(EKP_USER_URL, headers=_EKP_HEADERS) as response:
            if response.status != 200:
                raise SwissPostApiError(
                    f"HTTP {response.status} establishing the tracking session"
                )
            try:
                payload = await response.json(content_type=None)
            except ValueError as err:
                raise SwissPostApiError(
                    f"unparseable session handshake body ({err})"
                ) from err
            # Read through aiohttp's case-insensitive header mapping: Swiss Post
            # sends the header upper-case and a plain dict lookup misses it.
            token = response.headers.get(CSRF_HEADER)

        user_identifier = (
            payload.get("userIdentifier") if isinstance(payload, dict) else None
        )
        if not user_identifier:
            raise SwissPostApiError("session handshake returned no user identifier")
        if not token:
            raise SwissPostApiError("session handshake returned no CSRF token")

        self._user_identifier = user_identifier
        self._csrf_token = token
        _LOGGER.debug("Established an anonymous Swiss Post tracking session")

    async def async_lookup(self, tracking_code: str) -> list[Any]:
        """Look one tracking code up, re-establishing the session on a 403.

        Returns the raw — possibly empty — shipment list from surface A.
        """
        if not self.established:
            await self.async_establish()
        try:
            return await self._async_lookup(tracking_code)
        except SwissPostSessionExpired:
            _LOGGER.debug("Swiss Post session was rejected; re-establishing it")
            self.invalidate()
            await self.async_establish()
            try:
                return await self._async_lookup(tracking_code)
            except SwissPostSessionExpired as err:
                raise SwissPostApiError(
                    "tracking session rejected (HTTP 403) after re-establishing it"
                ) from err

    async def _async_lookup(self, tracking_code: str) -> list[Any]:
        """Run the two-call lookup on the current session."""
        # Passed as a query parameter so aiohttp URL-encodes it, which matters
        # here: the anonymous user id literally starts with "<[anonymous]>".
        params = {"userId": self._user_identifier}

        async with self._session.post(
            EKP_HISTORY_URL,
            params=params,
            headers={**_EKP_HEADERS, CSRF_HEADER: self._csrf_token},
            json={"searchQuery": tracking_code},
        ) as response:
            if response.status == 403:
                raise SwissPostSessionExpired
            if response.status != 200:
                raise SwissPostApiError(
                    f"HTTP {response.status} registering the tracking code"
                )
            try:
                registration = await response.json(content_type=None)
            except ValueError as err:
                raise SwissPostApiError(
                    f"unparseable registration body ({err})"
                ) from err

        digest = registration.get("hash") if isinstance(registration, dict) else None
        if not digest:
            raise SwissPostApiError("registration returned no hash")

        async with self._session.get(
            EKP_HISTORY_ITEM_URL.format(digest=digest),
            params=params,
            # No CSRF header here — this call is authorised by the cookie alone.
            headers=_EKP_HEADERS,
        ) as response:
            if response.status == 403:
                raise SwissPostSessionExpired
            if response.status != 200:
                raise SwissPostApiError(f"HTTP {response.status} reading the shipment")
            try:
                shipments = await response.json(content_type=None)
            except ValueError as err:
                raise SwissPostApiError(f"unparseable shipment body ({err})") from err

        if not isinstance(shipments, list):
            raise SwissPostApiError("unexpected shipment body (not a JSON list)")
        return shipments


class SwissPostApiClient:
    """Client for Swiss Post's two public tracking surfaces."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialise the client.

        ``session`` must be a **dedicated** aiohttp session with its own cookie
        jar: the ekp-web handshake stores a session cookie, and HA's shared
        session would then carry it into every other integration's requests.
        """
        self._session = session
        self._tracking_session = SwissPostSession(session)

    async def async_get_parcel(
        self, tracking_code: str, *, include_history: bool = False
    ) -> dict[str, Any] | None:
        """Fetch one parcel's tracking details.

        Returns the shipment dict for a known parcel, or ``None`` when Swiss
        Post resolves the code to nothing — an unknown code, or one not scanned
        yet. Any unexpected response raises :class:`SwissPostApiError`; network
        errors propagate as ``aiohttp.ClientError``.

        With ``include_history`` the event timeline is fetched from surface B
        and merged in. A failure there never fails the parcel: the timeline is
        a nice-to-have, the status is not.
        """
        shipments = await self._tracking_session.async_lookup(tracking_code)

        if not shipments:
            # The session is warm — ``async_lookup`` always runs the POST that
            # registers the code — so an empty list really is "Swiss Post does
            # not know this number (yet)", not a skipped registration.
            self._warn_empty_result(tracking_code)
            return None

        parcel = shipments[0]
        if not isinstance(parcel, dict):
            raise SwissPostApiError("unexpected shipment entry (not a JSON object)")

        if include_history:
            try:
                parcel["events"] = await self.async_get_history(tracking_code) or []
            except (SwissPostApiError, aiohttp.ClientError) as err:
                _LOGGER.warning(
                    "Swiss Post event history unavailable for %s: %s",
                    tracking_code,
                    err,
                )
                parcel["events"] = []

        return parcel

    async def async_get_history(self, tracking_code: str) -> list[Any] | None:
        """Fetch one parcel's event timeline from surface B.

        Returns the carrier's own event list, or ``None`` when the endpoint
        answers ``204``. Keyless and sessionless — a single POST.
        """
        async with self._session.post(
            EOS_HISTORY_URL,
            params={"culture": EOS_CULTURE},
            headers=_EOS_HEADERS,
            # Singular and capitalised. A plural/lowercase spelling is not
            # rejected — it is silently ignored with a 204.
            json={"Identifier": tracking_code},
        ) as response:
            if response.status == 204:
                # Never an unknown parcel — a 204 means this endpoint did not
                # recognise the body we sent.
                self._warn_history_204(tracking_code)
                return None
            if response.status != 200:
                raise SwissPostApiError(
                    f"HTTP {response.status} from the event history endpoint"
                )
            try:
                payload = await response.json(content_type=None)
            except ValueError as err:
                raise SwissPostApiError(f"unparseable history body ({err})") from err

        if not isinstance(payload, dict):
            raise SwissPostApiError("unexpected history body (not a JSON object)")

        data = payload.get("Data")
        if not isinstance(data, list) or not data:
            return []
        first = data[0]
        if not isinstance(first, dict):
            raise SwissPostApiError("unexpected history entry (not a JSON object)")
        events = first.get("History")
        return events if isinstance(events, list) else []

    @staticmethod
    def _warn_empty_result(tracking_code: str) -> None:
        """Warn once that Swiss Post resolved a tracking code to nothing."""
        if tracking_code in _empty_result_logged:
            return
        _empty_result_logged.add(tracking_code)
        _LOGGER.warning(
            "Swiss Post has no data for tracking code %s — either the number is "
            "unknown to them or the parcel has not been scanned yet. It stays "
            "tracked and fills in as soon as Swiss Post picks it up",
            tracking_code,
        )

    @staticmethod
    def _warn_history_204(tracking_code: str) -> None:
        """Warn once about a 204 from the history endpoint (a malformed request)."""
        if tracking_code in _history_204_logged:
            return
        _history_204_logged.add(tracking_code)
        _LOGGER.warning(
            "Swiss Post's event history endpoint answered 204 for %s. That means "
            "it did not understand our request, not that the parcel is unknown — "
            "the expected request format has probably changed. Please report it: "
            "https://github.com/ha-parcel-integrations/ha-swiss-post/issues/new",
            tracking_code,
        )
