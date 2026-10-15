"""Declare storage access before constructing command dependencies."""
from dataclasses import dataclass


@dataclass(frozen=True)
class CommandStoragePlan:
    source_readonly: bool
    adviser_readonly: bool


READ_ONLY_COMMANDS = frozenset({'status', 'conflicts', 'replies'})


def storage_plan(command: str) -> CommandStoragePlan:
    readonly = command in READ_ONLY_COMMANDS
    return CommandStoragePlan(source_readonly=readonly, adviser_readonly=readonly)
