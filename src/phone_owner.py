"""Phone-number normalization and owner sender-tier policy."""

import re

OWNER_TIER = "owner"
NON_OWNER_TIER = "other"


def normalize_phone(num: str) -> str:
    digits = re.sub(r"\D", "", num)
    return "1" + digits if len(digits) == 10 else digits


def phone_sender_tier(sender: str, owner_numbers: str) -> str:
    normalized_sender = normalize_phone(sender)
    owners = {normalize_phone(number.strip()) for number in owner_numbers.split(",")}
    if normalized_sender and normalized_sender in owners:
        return OWNER_TIER
    return NON_OWNER_TIER
