"""Open-Ant: harness-engineering personal AI agent runtime.

This package init runs before any submodule import — environment
tweaks that must precede third-party imports live here.
"""

import os

# Use litellm's bundled model cost map.  Without this, litellm tries to
# fetch the cost map from raw.githubusercontent.com on every startup,
# which fails offline and prints a scary WARNING before the logo.
# The bundled map is the same fallback litellm would use anyway.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
