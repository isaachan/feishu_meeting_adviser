"""Global lock order and migration lock requirements.

Every path that owns more than one managed lock acquires it in ascending order.
Database migration is an explicit write startup operation; read-only commands own no
migration lock and never invoke a migration runner.
"""
from enum import IntEnum


class LockRank(IntEnum):
    APPLICATION = 10
    MEETING = 20
    ADVISER = 30
    MAINTENANCE = 40


MIGRATION_LOCK_ORDER = {
    'source': (LockRank.APPLICATION, LockRank.MAINTENANCE),
    'adviser': (LockRank.ADVISER, LockRank.MAINTENANCE),
}


def validate_lock_order(*ranks: LockRank) -> None:
    if tuple(ranks) != tuple(sorted(set(ranks))):
        raise ValueError('managed locks must be acquired once in global order')
