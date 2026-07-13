"""Epic-3 review-gate configuration knobs (`app/config.py`).

`GAPFILLER_REVIEW_MODEL` selects the reviewer tier and defaults to the already
provider-resolved `MODEL` (byte-identical by default; verbatim when overridden — no
provider mangling). `GAPFILLER_REVIEW_REQUIRED` is a plain default-off bool via the
repo's `!= "0"` truthiness idiom.

The knobs are read from the environment at import time, so overrides are exercised by
reloading the module under a patched environment and restoring it afterwards. No network,
no model.

Runnable directly (`python tests/test_review_config.py`) or under pytest.
"""

from __future__ import annotations

import importlib
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config  # noqa: E402


def _reload(**env):
    """Reload app.config with the given env vars overlaid and return a SNAPSHOT of its
    module-level values, then restore the environment (and reload once more) so the default
    module state is left intact for other tests.

    The snapshot is essential: importlib.reload mutates the module object IN PLACE, so the
    restore-reload in `finally` would otherwise clobber the very values the caller wants to
    assert on (the returned reference would point at the already-restored module). Capturing
    a plain namespace under the patched env decouples the caller's assertions from the
    subsequent restore."""
    saved = {k: os.environ.get(k) for k in env}
    try:
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        reloaded = importlib.reload(config)
        return types.SimpleNamespace(**{
            k: getattr(reloaded, k) for k in dir(reloaded) if not k.startswith("__")
        })
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(config)


def test_review_model_defaults_to_model():
    # Unset override -> reviewer tier is exactly the (provider-resolved) main model.
    cfg = _reload(GAPFILLER_REVIEW_MODEL=None)
    assert cfg.REVIEW_MODEL == cfg.MODEL


def test_review_model_override():
    # An explicit slug is used verbatim — never provider-mangled.
    cfg = _reload(GAPFILLER_REVIEW_MODEL="some-vendor/review-model-9")
    assert cfg.REVIEW_MODEL == "some-vendor/review-model-9"
    assert cfg.REVIEW_MODEL != cfg.MODEL


def test_review_required_default_false():
    cfg = _reload(GAPFILLER_REVIEW_REQUIRED=None)
    assert cfg.REVIEW_REQUIRED is False


def test_review_required_parse():
    # Repo `!= "0"` idiom on a "0" default: any non-"0" is truthy; "0"/"" are false.
    assert _reload(GAPFILLER_REVIEW_REQUIRED="1").REVIEW_REQUIRED is True
    assert _reload(GAPFILLER_REVIEW_REQUIRED="true").REVIEW_REQUIRED is True
    assert _reload(GAPFILLER_REVIEW_REQUIRED="0").REVIEW_REQUIRED is False
    assert _reload(GAPFILLER_REVIEW_REQUIRED="").REVIEW_REQUIRED is False


def test_module_state_restored_after_reload():
    # The helper must leave the live module at its defaults for the rest of the suite.
    assert config.REVIEW_REQUIRED is False
    assert config.REVIEW_MODEL == config.MODEL


if __name__ == "__main__":
    import traceback

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn(); print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1; print(f"FAIL {fn.__name__}"); traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
