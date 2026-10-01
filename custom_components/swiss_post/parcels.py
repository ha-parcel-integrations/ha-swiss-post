"""Shared, source-agnostic parcel helpers.

Everything here is a **pure function** with no I/O and no Home Assistant objects
beyond the config entry's options. Both sources — the public tracking surface
(:mod:`.tracking.parcels`) and the SwissID account inbox
(:mod:`.account.parcels`) — build the same canonical parcel shape on top of
these helpers, so the timestamp parsing, the sort contract, the delivered
filter and the one-shot warning machinery live here once and cannot drift
between the two.

The per-source mapping (status vocabulary, field lookups, ``normalize_parcel``)
lives in each source package, not here.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
)

_LOGGER = logging.getLogger(__name__)

# Where users report a status we do not map yet, or a shape we have not seen
# populated. It must point at the carrier's own repo so the log line is
# copy-pasteable straight into a new issue; the ``?template=`` parameter keeps
# the report from coming back missing the version and the log line we need.
NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-swiss-post/issues/new"
    "?template=unrecognised_status.yml"
)

# Topics already warned about this HA session, so each one-shot warning fires at
# most once instead of on every poll. Shared across both sources deliberately:
# the same unmapped status seen on both surfaces is still one report.
_warned_once: set[str] = set()


def warn_once(topic: str, logger: logging.Logger, message: str, *args: Any) -> None:
    """Log ``message`` once per HA session, keyed on ``topic``.

    Used for the pre-1.0 "we have not seen this populated / mapped" reports.
    Field *names* are logged, never their values — a pickup point or a delivery
    window says where somebody lives and when a parcel is coming.
    """
    if topic in _warned_once:
        return
    _warned_once.add(topic)
    logger.warning(message, *args)


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string to an aware datetime, or ``None`` on failure.

    Naive values are treated as UTC so a list always sorts without crashing on
    a mixed set.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def to_iso_timestamp(value: Any) -> str | None:
    """Return a normalised ISO 8601 string for an ISO-ish API timestamp field.

    An unparseable value is passed through untouched rather than dropped —
    :func:`parse_iso` guards the consumers.
    """
    if value is None:
        return None
    text = str(value)
    parsed = parse_iso(text)
    return parsed.isoformat() if parsed is not None else text


def epoch_ms_to_iso(value: Any) -> str | None:
    """Return an ISO 8601 UTC string for an epoch-millisecond timestamp.

    The app backend (``mobserv``) stamps everything in epoch milliseconds —
    both the account overview's ``statusTimestamp`` and the detail timeline's
    event ``timestamp``. Anything not a finite number returns ``None``.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def format_dimensions(
    length: float | None, width: float | None, height: float | None
) -> dict[str, Any] | None:
    """Return the canonical ``dimensions`` dict, or ``None`` when incomplete.

    Units contract: **centimetres**, with ``text`` pre-formatted as
    ``"L x W x H cm"`` (integer values, lowercase ``x``) so dashboards can show
    a dimension without doing their own formatting.
    """
    if length is None or width is None or height is None:
        return None
    return {
        "length": length,
        "width": width,
        "height": height,
        "text": f"{int(length)} x {int(width)} x {int(height)} cm",
    }


def sort_parcels_by_ts(
    parcels: list[dict], key_field: str, *, descending: bool = False
) -> list[dict]:
    """Return normalised parcels sorted by the ISO timestamp at ``key_field``.

    The suite's sort contract: incoming/outgoing ascending on ``planned_from``,
    delivered descending on ``delivered_at``. Parcels whose value is missing or
    unparseable always sort to the end, regardless of ``descending``.
    """
    with_ts: list[tuple[datetime, dict]] = []
    without_ts: list[dict] = []
    for parcel in parcels:
        parsed = parse_iso(parcel.get(key_field))
        if parsed is None:
            without_ts.append(parcel)
        else:
            with_ts.append((parsed, parcel))
    with_ts.sort(key=lambda item: item[0], reverse=descending)
    return [parcel for _, parcel in with_ts] + without_ts


def apply_delivered_filter(parcels: list[dict], entry: ConfigEntry) -> list[dict]:
    """Trim the delivered list per the entry's retention option.

    ``parcels`` must already be sorted newest-first. ``days`` keeps deliveries
    from the last N days (an unparseable ``delivered_at`` is kept rather than
    silently dropped); the ``parcels`` type keeps the N most recent. Parcels
    stay *tracked* either way — this only controls what the delivered sensor
    shows.
    """
    options = entry.options
    filter_type = options.get(
        CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
    )
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(timezone.utc) - timedelta(days=amount)
        return [
            parcel
            for parcel in parcels
            if (parsed := parse_iso(parcel.get("delivered_at"))) is None
            or parsed >= cutoff
        ]
    return parcels[:amount]
