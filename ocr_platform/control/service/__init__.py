"""Compatibility façade for the pre-v0.3 control service import path.

New code must import from ``ocr_platform.control.domains``. This façade remains
for one release so existing Python integrations do not break during v0.3.
"""

import sys
from types import ModuleType

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


class _CompatibilityModule(ModuleType):
    """Mirror patched legacy globals into their owning domain modules.

    This matters for integrations that temporarily override service limits in
    process. New code should configure those limits through environment
    variables before importing the control application.
    """

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        for target in (_common, _jobs, _manifests, _model_profiles, _workers):
            if hasattr(target, name):
                setattr(target, name, value)


sys.modules[__name__].__class__ = _CompatibilityModule

__all__ = [name for name in globals() if not name.startswith("__")]
