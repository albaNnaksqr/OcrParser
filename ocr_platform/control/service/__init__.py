"""Compatibility façade for the pre-v0.3 control service import path.

New code must import from ``ocr_platform.control.domains``. This façade remains
for one release so existing Python integrations do not break during v0.3.
"""

import sys
from types import ModuleType

from .. import limits as __limits
from ..domains import common as _common
from ..domains.jobs import core as _jobs
from ..domains.manifests import core as _manifests
from ..domains.model_profiles import core as _model_profiles
from ..domains.workers import core as _workers
from ..domains.common import *
from ..domains.jobs.core import *
from ..domains.manifests.core import *
from ..domains.model_profiles.core import *
from ..domains.workers.core import *
from ..domains.model_profiles.commands import (
    upsert_model_profile as upsert_model_profile,
)
__jobs_core_command_leaves = {
    "record_event": _jobs.record_event,
    "record_log": _jobs.record_log,
}
from ..domains.jobs.commands import (
    record_event as record_event,
    record_log as record_log,
)
__job_command_wrappers = {
    "record_event": record_event,
    "record_log": record_log,
}
__manifest_core_command_leaves = {
    "register_remote_manifest": _manifests.register_remote_manifest,
}
from ..domains.manifests.commands import (
    register_remote_manifest as register_remote_manifest,
)
__manifest_command_wrappers = {
    "register_remote_manifest": register_remote_manifest,
}


class _CompatibilityModule(ModuleType):
    """Mirror patched legacy globals into their owning domain modules.

    This matters for integrations that temporarily override service limits in
    process. New code should configure those limits through environment
    variables before importing the control application.
    """

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        for target in (
            globals()["__limits"],
            _common,
            _jobs,
            _manifests,
            _model_profiles,
            _workers,
        ):
            if hasattr(target, name):
                if (
                    target is _jobs
                    and name in globals()["__job_command_wrappers"]
                    and value
                    is globals()["__job_command_wrappers"][name]
                ):
                    setattr(
                        target,
                        name,
                        globals()["__jobs_core_command_leaves"][name],
                    )
                elif (
                    target is _manifests
                    and name in globals()["__manifest_command_wrappers"]
                    and value
                    is globals()["__manifest_command_wrappers"][name]
                ):
                    setattr(
                        target,
                        name,
                        globals()["__manifest_core_command_leaves"][name],
                    )
                else:
                    setattr(target, name, value)


sys.modules[__name__].__class__ = _CompatibilityModule

__all__ = [name for name in globals() if not name.startswith("__")]
