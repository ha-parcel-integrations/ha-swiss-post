"""Client for the SwissID-authenticated Swiss Post account inbox (mobserv).

Isolated from the public tracking client: it carries a user-owned OAuth token
pair (plus the short-lived JWT ``id_token`` the inbox actually accepts as its
bearer) and knows nothing about the canonical parcel shape — normalisation
lives in :mod:`.parcels`.

The login is a standard Authorization-Code + PKCE flow against a public client
(no secret). Because SwissID may prompt for a second factor in the browser, the
flow is split: the config flow opens :meth:`build_authorize_url` in a browser,
the user pastes the redirect back, and :meth:`async_exchange_code` turns the
code into tokens. From then on :meth:`async_refresh` rotates the pair with no
user interaction.

The decisive trap: the inbox validates the **JWT ``id_token``**, not the opaque
``access_token`` (which it rejects as "Malformed token"). So the bearer is the
``id_token`` and it must be kept fresh — it lives one hour.
"""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable
import hashlib
import json
import logging
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import aiohttp

from ..const import (
    ACCOUNT_DETAIL_URL,
    ACCOUNT_OVERVIEW_URL,
    ACCOUNT_PRIVATE_ROLE,
    ACCOUNT_PROBLEM_HEADER,
    ACCOUNT_PROFILE_HEADER,
    ACCOUNT_TOKEN_REFRESH_MARGIN_SECONDS,
    OIDC_AUTHORIZE_URL,
    OIDC_CLIENT_ID,
    OIDC_REDIRECT_URI,
    OIDC_SCOPE,
    OIDC_TOKEN_URL,
    PROBLEM_NO_PRIVATE_USER,
    PROBLEM_SHOW_GTC,
    PROBLEM_SHOW_PROFILE_CHOOSER,
)

_LOGGER = logging.getLogger(__name__)

TokenCallback = Callable[[dict[str, str]], Awaitable[None]]
ProfileCallback = Callable[[str], Awaitable[None]]


class SwissPostAccountApiError(Exception):
    """An unexpected account API response, carrying no sensitive body."""

    def __init__(self, detail: str, *, status_code: int | None = None) -> None:
        """Store safe failure metadata without retaining the response body."""
        super().__init__(detail)
        self.status_code = status_code


class SwissPostAccountInvalidAuth(SwissPostAccountApiError):
    """The authorization code or login was rejected."""


class SwissPostAccountReauthRequired(SwissPostAccountApiError):
    """The stored refresh token can no longer be exchanged — needs re-login."""


class SwissPostAccountForbidden(SwissPostAccountApiError):
    """The inbox accepted the token but refuses to serve this account."""


class _ProfileChosen(Exception):
    """Internal: a Post profile was picked, so the call can be retried."""


def _problem_code(payload: Any) -> str:
    """Return the RFC 7807 code fields of an error body, never its prose.

    The inbox answers failures as ``urn:ch:post:app:problem:…`` bodies whose
    ``errorType``/``problemCode``/``errorCode`` say *why* it refused. Only those
    three are read: ``title``/``detail`` are free text that could name the user.
    """
    if not isinstance(payload, dict):
        return "no problem code"
    parts = [
        f"{key}={payload[key]}"
        for key in ("errorType", "problemCode", "errorCode")
        if isinstance(payload.get(key), (str, int))
    ]
    return ", ".join(parts) if parts else "no problem code"


async def _error_payload(response: aiohttp.ClientResponse) -> Any:
    """Return a failed response's parsed body, or ``None`` if it has none."""
    try:
        return await response.json(content_type=None)
    except (ValueError, aiohttp.ClientError):
        return None


def select_private_profile(payload: Any) -> str | None:
    """Return the private Post profile's id from a profile-chooser refusal.

    The refusal body lists every profile the SwissID is linked to. Only the
    enabled private one is picked: an employee or admin profile is a company's
    parcel inbox, not the household's. Nothing but the id is read — the rest of
    the entry describes the person or the company.
    """
    verification = (
        payload.get("klpProfileVerification") if isinstance(payload, dict) else None
    )
    profiles = verification.get("profiles") if isinstance(verification, dict) else None
    if not isinstance(profiles, list):
        return None
    for profile in profiles:
        if not isinstance(profile, dict) or profile.get("role") != ACCOUNT_PRIVATE_ROLE:
            continue
        if profile.get("enabled") is False:
            continue
        profile_id = profile.get("profileId")
        if isinstance(profile_id, str) and profile_id:
            return profile_id
    return None


def _pkce_pair() -> tuple[str, str]:
    """Return a ``(verifier, S256 challenge)`` PKCE pair."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def decode_jwt_claims(token: str | None) -> dict[str, Any]:
    """Return the (unverified) claim set of a JWT, or ``{}`` on any problem.

    Unverified is fine here: the token is used only as an opaque bearer and to
    read its own ``exp``/``sub``; HA never trusts these claims for a security
    decision.
    """
    if not token:
        return {}
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, IndexError, UnicodeDecodeError):
        return {}


def _tokens(payload: Any) -> dict[str, str]:
    """Pull the token triple out of a token-endpoint response."""
    if not isinstance(payload, dict):
        raise SwissPostAccountApiError("token response was not a JSON object")
    id_token = payload.get("id_token")
    refresh_token = payload.get("refresh_token")
    access_token = payload.get("access_token")
    # The id_token (bearer) and refresh_token are the two we cannot work
    # without; the access_token is stored for completeness but unused.
    if not isinstance(id_token, str) or not isinstance(refresh_token, str):
        raise SwissPostAccountApiError("token response missing id/refresh token")
    return {
        "id_token": id_token,
        "refresh_token": refresh_token,
        "access_token": access_token if isinstance(access_token, str) else "",
    }


class SwissPostAccountClient:
    """SwissID OAuth client for the Swiss Post account inbox."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        id_token: str | None = None,
        refresh_token: str | None = None,
        device_id: str | None = None,
        klp_profile_id: str | None = None,
        token_callback: TokenCallback | None = None,
        profile_callback: ProfileCallback | None = None,
    ) -> None:
        """Initialise the client with entry-owned tokens and a persist hook."""
        self._session = session
        self._id_token = id_token
        self._refresh_token = refresh_token
        self._device_id = device_id
        self._klp_profile_id = klp_profile_id
        self._token_callback = token_callback
        self._profile_callback = profile_callback

    @staticmethod
    def build_authorize_url() -> tuple[str, str, str]:
        """Return ``(authorize_url, code_verifier, state)`` for the browser step.

        The caller shows the URL, the user logs in (and clears any second
        factor), and the browser lands on the redirect URI with ``code`` and
        ``state`` on the query string for the user to paste back.
        """
        verifier, challenge = _pkce_pair()
        state = secrets.token_urlsafe(24)
        params = {
            "response_type": "code",
            "client_id": OIDC_CLIENT_ID,
            "redirect_uri": OIDC_REDIRECT_URI,
            "scope": OIDC_SCOPE,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "prompt": "login",
        }
        return f"{OIDC_AUTHORIZE_URL}?{urlencode(params)}", verifier, state

    async def _token_request(self, data: dict[str, str]) -> dict[str, str]:
        """POST to the token endpoint and return the parsed token triple."""
        async with self._session.post(
            OIDC_TOKEN_URL,
            data=data,
            headers={"Accept": "application/json"},
        ) as response:
            if response.status in (400, 401):
                # A bad/expired code or a dead refresh chain both land here.
                raise SwissPostAccountInvalidAuth(
                    "token endpoint rejected the request", status_code=response.status
                )
            if response.status != 200:
                raise SwissPostAccountApiError(
                    "token endpoint request failed", status_code=response.status
                )
            try:
                payload = await response.json(content_type=None)
            except ValueError as err:
                raise SwissPostAccountApiError(
                    "token endpoint returned invalid JSON"
                ) from err
        return _tokens(payload)

    async def async_exchange_code(self, code: str, verifier: str) -> dict[str, str]:
        """Exchange an authorization code for the initial token triple."""
        tokens = await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": OIDC_REDIRECT_URI,
                "client_id": OIDC_CLIENT_ID,
                "code_verifier": verifier,
            }
        )
        self._id_token = tokens["id_token"]
        self._refresh_token = tokens["refresh_token"]
        return tokens

    async def async_refresh(self) -> dict[str, str]:
        """Rotate the token pair; persist the new one via the callback.

        The refresh token rotates on every call, so the new one must be stored
        or the chain is lost. A rejected refresh means the chain is dead and
        the user has to log in again.
        """
        if not self._refresh_token:
            raise SwissPostAccountReauthRequired("no refresh token stored")
        try:
            tokens = await self._token_request(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                    "client_id": OIDC_CLIENT_ID,
                    "scope": OIDC_SCOPE,
                }
            )
        except SwissPostAccountInvalidAuth as err:
            raise SwissPostAccountReauthRequired("refresh token rejected") from err
        self._id_token = tokens["id_token"]
        self._refresh_token = tokens["refresh_token"]
        if self._token_callback is not None:
            await self._token_callback(tokens)
        return tokens

    def _id_token_is_fresh(self) -> bool:
        """Whether the stored id_token is valid past the refresh margin."""
        if not self._id_token:
            return False
        exp = decode_jwt_claims(self._id_token).get("exp")
        if not isinstance(exp, (int, float)):
            return False
        return time.time() < exp - ACCOUNT_TOKEN_REFRESH_MARGIN_SECONDS

    async def async_get_overview(self) -> dict[str, Any]:
        """Return the raw ``overview`` payload for the logged-in user.

        Refreshes the id_token first when it is close to expiry, and refreshes
        once more and retries on a surprise 401 — a token can still be rejected
        between the freshness check and the call. A first call for a SwissID
        with more than one Post profile is refused until a profile is chosen;
        that choice is made from the refusal itself and the call retried once.
        """
        if not self._id_token_is_fresh():
            await self.async_refresh()
        try:
            return await self._overview_request()
        except SwissPostAccountReauthRequired:
            # Token rejected despite looking fresh — rotate once and retry.
            await self.async_refresh()
        except _ProfileChosen:
            # The retry now carries the profile header, so the same refusal
            # cannot repeat: the picker only runs while no profile is known.
            pass
        return await self._overview_request()

    async def _overview_request(self) -> dict[str, Any]:
        """Perform the authenticated overview GET on the current id_token."""
        headers = {
            "Authorization": f"Bearer {self._id_token}",
            "x-device-id": self._device_id or "",
            "Accept": "application/json",
        }
        if self._klp_profile_id:
            headers[ACCOUNT_PROFILE_HEADER] = self._klp_profile_id
        async with self._session.get(ACCOUNT_OVERVIEW_URL, headers=headers) as response:
            if response.status != 200:
                await self._raise_for_refusal(response)
            try:
                payload = await response.json(content_type=None)
            except ValueError as err:
                raise SwissPostAccountApiError("inbox returned invalid JSON") from err
        if not isinstance(payload, dict):
            raise SwissPostAccountApiError("inbox payload was not a JSON object")
        return payload

    async def _raise_for_refusal(self, response: aiohttp.ClientResponse) -> None:
        """Turn a refused inbox call into the error that fits its cause.

        Swiss Post names the cause in the ``x-problem-id`` response header, and
        it is what tells a recoverable refusal (a profile still to be chosen)
        apart from a dead token or a dead end.
        """
        payload = await _error_payload(response)
        problem = response.headers.get(ACCOUNT_PROBLEM_HEADER) or ""
        detail = f"HTTP {response.status}, {problem or _problem_code(payload)}"

        if problem == PROBLEM_SHOW_PROFILE_CHOOSER and not self._klp_profile_id:
            profile_id = select_private_profile(payload)
            if profile_id is None:
                raise SwissPostAccountForbidden(
                    f"inbox listed no private Post profile to use ({detail})",
                    status_code=response.status,
                )
            _LOGGER.debug("Swiss Post: using the private Post profile of this SwissID")
            self._klp_profile_id = profile_id
            if self._profile_callback is not None:
                await self._profile_callback(profile_id)
            raise _ProfileChosen
        if problem == PROBLEM_NO_PRIVATE_USER:
            raise SwissPostAccountForbidden(
                f"this SwissID has no private Post profile ({detail})",
                status_code=response.status,
            )
        if problem == PROBLEM_SHOW_GTC:
            raise SwissPostAccountForbidden(
                f"Swiss Post wants its terms accepted in the Post app first ({detail})",
                status_code=response.status,
            )
        if response.status == 401:
            raise SwissPostAccountReauthRequired(
                f"inbox rejected the id_token ({detail})",
                status_code=response.status,
            )
        if response.status == 403:
            # Not a token problem: the token passed validation and the account
            # was still refused, so signing in again cannot help.
            raise SwissPostAccountForbidden(
                f"inbox refused this account ({detail})",
                status_code=response.status,
            )
        raise SwissPostAccountApiError(
            f"inbox request failed ({detail})", status_code=response.status
        )

    async def async_get_detail(self, mailpiece_id: str) -> dict[str, Any] | None:
        """Return the full per-parcel record, or ``None`` when there is none.

        The enrichment the inbox summary lacks: event timeline, weight and
        dimensions. This call carries **no bearer** — it is the same anonymous
        lookup the app uses for a bare tracking number, so a parcel the public
        surface does not know yet (one not handed in) simply answers ``404`` and
        the parcel keeps its summary-only fields instead of failing the poll.
        """
        if not mailpiece_id:
            return None
        async with self._session.get(
            ACCOUNT_DETAIL_URL,
            params={"mailpieceId": mailpiece_id, "dontFollow": "true"},
            headers={
                "x-device-id": self._device_id or "",
                "Accept": "application/json",
            },
        ) as response:
            if response.status == 404:
                return None
            if response.status != 200:
                raise SwissPostAccountApiError(
                    "parcel detail request failed", status_code=response.status
                )
            try:
                payload = await response.json(content_type=None)
            except ValueError as err:
                raise SwissPostAccountApiError(
                    "parcel detail returned invalid JSON"
                ) from err
        if not isinstance(payload, dict):
            raise SwissPostAccountApiError("parcel detail was not a JSON object")
        # The record sits under "detail"; an empty match answers 200 with none.
        detail = payload.get("detail")
        return detail if isinstance(detail, dict) else None
