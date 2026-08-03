"""Sample Swiss Post API payloads shared by the test modules.

Shaped after real, live-captured responses with every identifying field either
blanked or replaced by an invented value — the tracking numbers below are made
up, not somebody's parcel. Kept in one module rather than inline in each test:
when the payload shape turns out to differ from what we assumed, there is then
exactly one place to fix.

Two surfaces (see ``api.py``): ``shipment_*`` samples come from ekp-web and are
what ``normalize_parcel`` maps; ``history_response`` is the postlogistics event
timeline, whose ``History`` list ``api.py`` merges into the shipment's
``events``.
"""
from __future__ import annotations

ACTIVE_CODE = "990012345678901234"
DELIVERED_CODE = "990098765432109876"


def event(status: str, timestamp: str, description: str, event_id: int = 1) -> dict:
    """One entry of Swiss Post's event timeline (surface B)."""
    return {
        "Id": event_id,
        "Status": status,
        "NextStatus": "",
        "TimeStamp": timestamp,
        "Description": description,
        "FullDescription": "",
        "GeoLocation": None,
        "City": "",
    }


# The event vocabulary is near-constant in real data: "PST" on almost
# everything, including the delivery itself. That is exactly why build_history
# never maps it to a ParcelStatus.
EVENTS = [
    event("PST", "2026-04-14T16:13:24Z", "Dateneinlieferung durch Aufgeber", 1),
    event("PST", "2026-04-15T08:57:24Z", "Sortiert", 2),
    event("PST", "2026-04-15T13:04:16Z", "Sortiert für die Zustellung", 3),
    event("NTF", "2026-04-16T04:37:12Z", "Verlad Fahrzeug", 4),
    event("PST", "2026-04-16T08:28:29Z", "Zugestellt an Domiziladresse", 5),
]


def history_response(code: str = DELIVERED_CODE, events: list | None = None) -> dict:
    """A full surface-B response envelope."""
    return {
        "Type": 3,
        "Data": [
            {
                "Identifier": code,
                "DriveAndArrive": None,
                "History": EVENTS if events is None else events,
            }
        ],
    }


def delivered_sample(code: str = DELIVERED_CODE) -> dict:
    """A representative ekp-web shipment for a delivered parcel."""
    return {
        "shipmentNumber": code,
        "formattedShipmentNumber": code,
        "identity": "REDACTED",
        "shipmentId": 3413029971,
        "product": "PARCEL.*.1",
        "status": "PARCEL.*.1.6",
        "additionalStatus": "1",
        "globalStatus": "DELIVERED",
        "calculatedDeliveryDate": "2026-04-16T00:00:00+02:00",
        "deliveryDate": "2026-04-16T10:28:29+02:00",
        "attemptedDeliveryDate": None,
        "deliveryRange": None,
        "deliveryTimeWindow": None,
        "deliveryTimeInterval": None,
        "lastEventDateTime": "2026-04-16T10:28:29+02:00",
        "sendingDateTime": "2026-04-15T10:57:24+02:00",
        "creationDateTime": "2026-04-14T20:14:07+02:00",
        "physicalProperties": {
            "dimension1": 400,
            "dimension2": 250,
            "dimension3": 155,
            "weight": 1140,
        },
        "addressee": {
            "name1": None,
            "name2": None,
            "name3": None,
            "street": None,
            "number": None,
            "zip": "3000",
            "city": "Bern",
            "streetAndNumber": "",
        },
        "addresseeType": "OTHER",
        "originalAddressee": None,
        "postboxDeliveryStatus": "NOT_POSSIBLE",
        "postOfficeBox": False,
        "deliveryPostOfficeZip": None,
        "expectedDeliveryZip": None,
        "delivered": True,
        "returned": False,
        "closed": True,
        "sorted": True,
        "signatureRequired": False,
        "signature": None,
        "customsShipment": False,
        "customsPaid": None,
        "internationalImport": False,
        "internationalExport": False,
        "investigationFlag": False,
        "returnCount": 0,
        "proofOfDeliveryImageId": None,
        "avis": None,
        "displayedAvisCode": None,
        "senderCountry": "CH",
        "recipientCountry": "CH",
        "sender": None,
        "source": "PARCEL",
        "additionalServices": [],
        "availableSingleSettings": [],
        # Always empty on this surface; api.py fills it from surface B when the
        # history option is on.
        "events": [],
    }


def active_sample(code: str = ACTIVE_CODE) -> dict:
    """An out-for-delivery parcel."""
    sample = delivered_sample(code)
    sample.update(
        {
            "globalStatus": "IN_DELIVERY",
            "status": "PARCEL.*.1.4",
            "deliveryDate": None,
            "delivered": False,
            "closed": False,
            "calculatedDeliveryDate": "2026-04-16T00:00:00+02:00",
            "events": [],
        }
    )
    return sample


def windowed_sample(code: str = ACTIVE_CODE) -> dict:
    """A parcel with a real delivery window rather than a day estimate.

    Never seen in live data — this is the shape the API documents and the one
    ``_delivery_window`` warns about the first time it appears.
    """
    sample = active_sample(code)
    sample["deliveryRange"] = {
        "start": "2026-04-16T13:00:00+02:00",
        "end": "2026-04-16T15:00:00+02:00",
    }
    return sample


def pickup_sample(code: str = ACTIVE_CODE) -> dict:
    """A parcel waiting at a post office.

    Also unseen live: no ``globalStatus`` token for it is known, so the status
    here is deliberately one we do not map — that is what a real pickup parcel
    is expected to look like until a user reports the real value.
    """
    sample = active_sample(code)
    sample.update(
        {
            "globalStatus": "AT_POST_OFFICE",
            "deliveryPostOfficeZip": "3011",
            "avis": "AVIS",
            "displayedAvisCode": "1",
        }
    )
    return sample
