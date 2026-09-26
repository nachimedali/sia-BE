"""Nothing reaches around the seam.

The ports are only worth having if the call sites actually use them. A single
`from trends.models import TrendItem` in a new feature re-creates the coupling
M0 just removed, and it would not be noticed until Phase 12 tried to delete the
app and the import broke.

Two sets are exempt and both are temporary:

* `trends/` and `ai/` are the code that is *moving*. They may use each other
  freely; they leave together.
* The competitor surface in `analytics/` is a documented deferral — see the
  exception list below.
"""

from __future__ import annotations

import ast
from pathlib import Path

from django.conf import settings

ROOT = Path(__file__).resolve().parents[2]

#: Modules that are moving to trendgen. Internal coupling here is fine.
MOVING = ("trends", "ai")

#: Everything else, derived from the app registry rather than typed out. A
#: hand-maintained list is the very failure this file exists to catch: `common`,
#: `accounts` and `categories` were all missing from the first version of it, so
#: an import there would have passed silently, and a newly registered app
#: defaulted to unguarded.
GUARDED = tuple(sorted(set(settings.LOCAL_APPS) - set(MOVING) - {"integrations"}))

#: What may not be imported from a guarded module.
FORBIDDEN_PREFIXES = ("trends.", "ai.services.pipeline", "ai.services.costing")

#: Documented, dated exceptions. Each one is a task in trendgen's todo.md.
EXCEPTIONS = {
    # Competitor tracking is a trend source kind whose API is a ModelSerializer
    # over `TrendSource`. Porting it is a response-shape change, not an
    # indirection, so it is deferred to Phase 12 (M0-06) rather than rushed
    # into the seam.
    "analytics/views.py": ("trends.services",),
    "analytics/serializers.py": ("trends.models",),
}


def guarded_files() -> list[Path]:
    files: list[Path] = []
    for app in GUARDED:
        files.extend(
            path
            for path in (ROOT / app).rglob("*.py")
            if "migrations" not in path.parts
            and "__pycache__" not in path.parts
            and "tests" not in path.parts
        )
    return files


def forbidden_imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        module: str | None = None
        if isinstance(node, ast.ImportFrom):
            module = node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(FORBIDDEN_PREFIXES):
                    found.add(alias.name)
            continue
        if module and module.startswith(FORBIDDEN_PREFIXES):
            found.add(module)
    return found


def test_no_guarded_module_reaches_past_the_ports():
    offenders: dict[str, set[str]] = {}
    for path in guarded_files():
        rel = str(path.relative_to(ROOT))
        found = forbidden_imports(path)
        allowed = set(EXCEPTIONS.get(rel, ()))
        unexpected = (
            {name for name in found if not name.startswith(tuple(allowed))} if allowed else found
        )
        if unexpected:
            offenders[rel] = unexpected
    assert not offenders, (
        f"{offenders} import the moving apps directly. Use integrations.trendfeed / "
        "integrations.generation, or add a dated exception with a todo.md task."
    )


def test_every_exception_is_still_real():
    """A stale exception silently re-opens the rule for a file that has moved on."""
    for rel, expected in EXCEPTIONS.items():
        path = ROOT / rel
        assert path.exists(), f"{rel} is excepted but gone"
        found = forbidden_imports(path)
        assert found, f"{rel} no longer imports the moving apps; drop its exception"
        assert all(name.startswith(tuple(expected)) for name in found), (
            f"{rel} now imports {found}, wider than its recorded exception {expected}"
        )


def test_the_guarded_set_covers_every_local_app():
    """Derived, so this is a statement about the derivation rather than a list
    to maintain — but it is worth failing loudly if the registry stops being
    readable from here."""
    assert GUARDED, "no apps guarded; settings.LOCAL_APPS did not resolve"
    assert set(GUARDED) | set(MOVING) | {"integrations"} == set(settings.LOCAL_APPS)
