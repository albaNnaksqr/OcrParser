"""Compatibility façade for the pre-v0.3 control service import path.

New code must import from ``ocr_platform.control.domains``. This façade remains
for one release so existing Python integrations do not break during v0.3.
"""

import sys
from types import ModuleType

from .. import limits as __limits
from ..domains import common as _common
from ..domains.jobs import core as _jobs
from ..domains.jobs import counters as __job_counters
from ..domains.jobs import events as __job_events
from ..domains.jobs import lifecycle as __job_lifecycle
from ..domains.jobs import logs as __job_logs
from ..domains.jobs import projection as __job_projection
from ..domains.manifests import core as _manifests
from ..domains.manifests import construction as __manifest_construction
from ..domains.manifests import freeze as __manifest_freeze
from ..domains.manifests import integrity as __manifest_integrity
from ..domains.manifests import paths as __manifest_paths
from ..domains.manifests import projection as __manifest_projection
from ..domains.manifests import use_cases as __manifest_use_cases
from ..domains.model_profiles import core as _model_profiles
from ..domains.workers import core as _workers
from ..domains.workers import assignment as __worker_assignment
from ..domains.workers import eligibility as __worker_eligibility
from ..domains.workers import identity as __worker_identity
from ..domains.workers import preflight as __worker_preflight
from ..domains.workers import projection as __worker_projection
from ..domains.workers import registration as __worker_registration
from ..domains.workers import use_cases as __worker_use_cases
from ..domains.common import *
from ..domains.jobs.core import *
from ..domains.manifests.core import *
from ..domains.model_profiles.core import *
from ..domains.workers.core import *
from ..domains.model_profiles.commands import (
    upsert_model_profile as upsert_model_profile,
)
from ..settings import (
    ALLOW_SAVED_MODEL_PROFILE_KEYS_ENV as ALLOW_SAVED_MODEL_PROFILE_KEYS_ENV,
    DISABLE_SAVED_MODEL_PROFILE_KEYS_ENV as DISABLE_SAVED_MODEL_PROFILE_KEYS_ENV,
)
__jobs_core_compat_wrappers = {
    "archive_job": _jobs.archive_job,
    "create_job": _jobs.create_job,
    "delete_job": _jobs.delete_job,
    "record_event": _jobs.record_event,
    "record_log": _jobs.record_log,
    "request_stop": _jobs.request_stop,
}
__jobs_owned_leaves = {
    "archive_job": __job_lifecycle.archive,
    "create_job": __job_lifecycle.create,
    "delete_job": __job_lifecycle.delete,
    "record_event": __job_events.record_event,
    "record_log": __job_logs.record,
    "request_stop": __job_lifecycle.request_stop,
}
__jobs_owned_targets = {
    "archive_job": (__job_lifecycle, "archive"),
    "create_job": (__job_lifecycle, "create"),
    "delete_job": (__job_lifecycle, "delete"),
    "record_event": (__job_events, "record_event"),
    "record_log": (__job_logs, "record"),
    "request_stop": (__job_lifecycle, "request_stop"),
}
from ..domains.jobs.commands import (
    archive_job as archive_job,
    create_job as create_job,
    delete_job as delete_job,
    record_event as record_event,
    record_log as record_log,
    request_stop as request_stop,
)
__job_command_wrappers = {
    "archive_job": archive_job,
    "create_job": create_job,
    "delete_job": delete_job,
    "record_event": record_event,
    "record_log": record_log,
    "request_stop": request_stop,
}
__manifest_core_command_leaves = {
    "register_remote_manifest": _manifests.register_remote_manifest,
}
__manifest_owned_leaves = {
    "register_remote_manifest": __manifest_construction.register_remote_manifest,
}
__manifest_owned_targets = {
    "register_remote_manifest": (
        __manifest_construction,
        "register_remote_manifest",
    ),
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
            globals()["__job_counters"],
            globals()["__job_events"],
            globals()["__job_lifecycle"],
            globals()["__job_logs"],
            globals()["__job_projection"],
            _manifests,
            globals()["__manifest_construction"],
            globals()["__manifest_freeze"],
            globals()["__manifest_integrity"],
            globals()["__manifest_paths"],
            globals()["__manifest_projection"],
            globals()["__manifest_use_cases"],
            _model_profiles,
            _workers,
            globals()["__worker_assignment"],
            globals()["__worker_eligibility"],
            globals()["__worker_identity"],
            globals()["__worker_preflight"],
            globals()["__worker_projection"],
            globals()["__worker_registration"],
            globals()["__worker_use_cases"],
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
                        globals()["__jobs_core_compat_wrappers"][name],
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
        owned_target = globals()["__jobs_owned_targets"].get(name)
        if owned_target is not None:
            target_module, target_name = owned_target
            replacement = value
            if value is globals()["__job_command_wrappers"].get(name):
                replacement = globals()["__jobs_owned_leaves"][name]
            setattr(target_module, target_name, replacement)
        manifest_owned_target = globals()["__manifest_owned_targets"].get(name)
        if manifest_owned_target is not None:
            target_module, target_name = manifest_owned_target
            replacement = value
            if value is globals()["__manifest_command_wrappers"].get(name):
                replacement = globals()["__manifest_owned_leaves"][name]
            setattr(target_module, target_name, replacement)


sys.modules[__name__].__class__ = _CompatibilityModule

__all__ = [name for name in globals() if not name.startswith("__")]
