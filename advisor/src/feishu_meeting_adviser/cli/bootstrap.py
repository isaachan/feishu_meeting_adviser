"""Declare storage access before constructing command dependencies."""
from dataclasses import dataclass


@dataclass(frozen=True)
class CommandStoragePlan:
    source_readonly: bool | None   # None: the command does not touch the source store
    adviser_readonly: bool | None


PLANS = {
    'session-new': CommandStoragePlan(False, False),
    'session-list': CommandStoragePlan(None, True),
    'session-end': CommandStoragePlan(True, False),
    'status': CommandStoragePlan(True, True),
    'consume': CommandStoragePlan(False, False),
    'replies': CommandStoragePlan(None, True),
    'resolve-reply': CommandStoragePlan(None, False),
}


def storage_plan(command: str) -> CommandStoragePlan:
    return PLANS[command]
