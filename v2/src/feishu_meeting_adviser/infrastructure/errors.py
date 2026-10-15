"""Safe infrastructure errors shared without importing application modules."""


class LifecycleError(Exception):
    pass


class LockBusy(LifecycleError):
    pass
