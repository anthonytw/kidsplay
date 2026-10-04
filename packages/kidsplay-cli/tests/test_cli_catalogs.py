"""The cli catalogs are current: .pot matches the sources, .mo matches the .po."""

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "i18n.py"


def _load_tool() -> object:
    spec = importlib.util.spec_from_file_location("kidsplay_i18n_tool", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_catalogs_are_up_to_date() -> None:
    """Fails with the command to run when a string was added or a .po edited."""
    tool = _load_tool()
    problems = tool.check(tool.DOMAINS["cli"])  # ty: ignore[unresolved-attribute] # loaded from a path, so its attributes are dynamic
    assert not problems, "\n".join(problems)
