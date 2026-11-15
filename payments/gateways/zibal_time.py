"""Interpret Zibal's ISODate without inventing the timezone of offset-free values.

The official IPG OpenAPI labels paidAt date-time, but its examples have no
offset and it specifies no timezone. Keep configuration empty until confirmed.
"""

from dataclasses import dataclass
from datetime import datetime, timezone as datetime_timezone
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.utils import timezone


_ISO_DATE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})?"
)


@dataclass(frozen=True)
class GatewayPaymentTime:
    value: datetime | None = None
    issue: str = ""
    timezone_name: str = ""
    source: str = ""

    def audit(self) -> dict:
        return {
            "state": "manual_review" if self.issue else "valid",
            "reason": self.issue,
            "timezone": self.timezone_name,
            "source": self.source,
            "normalized_paid_at": self.value.isoformat() if self.value else None,
        }


def confirmed_zone(name: str) -> ZoneInfo:
    """Only explicit IANA identifiers, never the server/Django local timezone."""
    if not isinstance(name, str) or not name or name != name.strip():
        raise ValueError("An explicitly confirmed IANA timezone is required.")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValueError("Invalid IANA timezone.") from exc


def parse_gateway_paid_at(value, *, confirmed_timezone=None, now=None) -> GatewayPaymentTime:
    if not isinstance(value, str) or not _ISO_DATE.fullmatch(value):
        return GatewayPaymentTime(issue="invalid_timestamp")
    if value.endswith("-00:00"):
        # RFC 3339 -00:00 means that the local offset is unknown.
        return GatewayPaymentTime(issue="unknown_offset")
    if value[-6:-5] in ("+", "-") and (
        int(value[-5:-3]) > 23 or int(value[-2:]) > 59
    ):
        # fromisoformat normalizes malformed offsets such as +03:60 to +04:00.
        # Gateway data must be validated, never repaired into a valid instant.
        return GatewayPaymentTime(issue="invalid_timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timezone.is_naive(parsed):
            name = (
                getattr(settings, "ZIBAL_PAID_AT_TIMEZONE", "")
                if confirmed_timezone is None else confirmed_timezone
            )
            if not name:
                return GatewayPaymentTime(issue="timezone_unconfirmed")
            try:
                zone = confirmed_zone(name)
            except ValueError:
                return GatewayPaymentTime(issue="invalid_timezone_configuration")
            # make_aware alone accepts DST gaps/folds. Round-trip both folds;
            # accept only a unique real instant in the confirmed timezone.
            candidates = {
                aware.astimezone(datetime_timezone.utc)
                for fold in (0, 1)
                if (aware := parsed.replace(tzinfo=zone, fold=fold))
                .astimezone(datetime_timezone.utc).astimezone(zone).replace(tzinfo=None) == parsed
            }
            if len(candidates) != 1:
                return GatewayPaymentTime(
                    issue="ambiguous_local_time" if candidates else "nonexistent_local_time",
                    timezone_name=name, source="configuration",
                )
            normalized = candidates.pop()
            source = "configuration"
        else:
            name = str(parsed.tzinfo)
            normalized = parsed.astimezone(datetime_timezone.utc)
            source = "offset"
        if normalized.year < 1000:
            # MariaDB DATETIME cannot store this value. Preserve the original in
            # the audit instead of losing the entire receipt to a database error.
            return GatewayPaymentTime(issue="invalid_timestamp")
        issue = "future_timestamp" if normalized > (now or timezone.now()) else ""
        return GatewayPaymentTime(normalized, issue, name, source)
    except (ValueError, OverflowError, TypeError):
        return GatewayPaymentTime(issue="invalid_timestamp")
