"""Model registry.

Alembic autogenerate and the test harness both need every mapped class imported
before ``Base.metadata`` is complete. Modules register here explicitly rather
than relying on import side effects, so a forgotten import fails loudly at the
registry instead of silently producing an empty migration.
"""

from __future__ import annotations

import importlib

# Model modules, in dependency order. Extended as each phase lands.
MODEL_MODULES: tuple[str, ...] = ("verity.modules.identity.models",)


def import_all_models() -> None:
    for module in MODEL_MODULES:
        importlib.import_module(module)
