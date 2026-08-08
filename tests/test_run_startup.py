"""run.py's startup path -- the code between "launch" and "the UI is up".

Nothing else covers it, because it is the one module the test suite never
imports: it is the entrypoint, so every test reaches the app *past* it. That
gap is not theoretical. On 2026-08-08 a refactor moved run.py's module-level

    from forex_trader.config import USER_DATA_DIR as _USER_DATA

inside _setup_logging(), where it became a local -- leaving _ensure_data_dirs()
reading a global that no longer existed. Every start died with NameError
before the UI bound its port, and the only trace was a traceback in
restart.log, which nobody reads until the app is already missing. The app sat
down for hours while the Telegram panel's Restart button appeared to do
nothing at all.

No order is placed, closed or modified here: main() is guarded behind
__name__ == "__main__", so importing run.py runs its definitions only.
"""
import builtins
import importlib.util
import symtable
from pathlib import Path

import pytest

RUN_PY = Path(__file__).resolve().parent.parent / "run.py"


def _load_run_module():
    spec = importlib.util.spec_from_file_location("forex_run_entrypoint", RUN_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def run_module():
    return _load_run_module()


def test_ensure_data_dirs_creates_the_sessions_directory(run_module, tmp_path, monkeypatch):
    """The exact call that crashed. _ensure_data_dirs imports USER_DATA_DIR
    itself at call time, so monkeypatching the config attribute is enough to
    keep the test off the real user data directory."""
    import forex_trader.config as cfg
    monkeypatch.setattr(cfg, "USER_DATA_DIR", tmp_path)

    run_module._ensure_data_dirs()

    assert (tmp_path / "data" / "sessions").is_dir()


def test_no_startup_function_reads_a_name_the_module_does_not_define(run_module):
    """The general form of the same bug: any top-level function in run.py
    referencing a global that isn't defined at module scope or a builtin.

    Uses symtable rather than a hand-rolled AST walk so the scope rules
    (locals, closures, comprehensions, explicit `global`) are Python's own.
    A name imported inside some *other* function is correctly reported --
    that is precisely what a moved import leaves behind.
    """
    module_scope = symtable.symtable(RUN_PY.read_text(), str(RUN_PY), "exec")
    available = set(vars(run_module)) | set(dir(builtins))

    missing: list[str] = []
    for child in module_scope.get_children():
        if child.get_type() != "function":
            continue
        for symbol in child.get_symbols():
            if symbol.is_global() and symbol.get_name() not in available:
                missing.append(f"{child.get_name()}() reads undefined "
                               f"global {symbol.get_name()!r}")

    assert not missing, (
        "run.py would raise NameError at startup:\n  " + "\n  ".join(missing))
