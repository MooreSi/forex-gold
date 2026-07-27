"""
Manual ML model snapshot transfer — pure packaging/unpacking helpers.

Deliberately NOT part of the continuous sync loop: each node trains its own
models independently (see the design discussion — merging live training
data across a network multiplies the model-version fragility that was
already an issue within a single machine). This is a one-shot, user-
triggered copy of the joblib/pkl model files from one node's DATA_DIR to
the other's, for seeding a freshly-set-up node with a more mature model.

The actual websocket send/receive loop lives in server.py and client.py
(mirroring the proven binary-frame pattern already used in
forex_trader.remote for app updates); this module only packages bytes.
"""
from __future__ import annotations

import io
import logging
import zipfile
from pathlib import Path

log = logging.getLogger("sync")

# Every engine's model file glob, relative to DATA_DIR. Kept as an explicit
# allowlist (not "every file in DATA_DIR") so credentials, session files,
# and databases can never accidentally end up in a model transfer.
_MODEL_GLOBS = (
    "bo_ml_*.joblib",
    "ml_signal_*.joblib",
    "re_ml_*.pkl",
)


def _model_files(data_dir: Path) -> list[Path]:
    files: list[Path] = []
    for pattern in _MODEL_GLOBS:
        files.extend(sorted(data_dir.glob(pattern)))
    return files


def package_models(data_dir: Path) -> tuple[bytes, list[str]]:
    """Zip up every known model file under data_dir. Returns (zip_bytes, names)."""
    files = _model_files(data_dir)
    buf = io.BytesIO()
    names = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, arcname=f.name)
            names.append(f.name)
    return buf.getvalue(), names


def unpack_models(zip_bytes: bytes, data_dir: Path) -> list[str]:
    """Extract a model snapshot into data_dir, overwriting existing files of
    the same name. Returns the list of filenames written."""
    written = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for info in zf.infolist():
            # Defense in depth: reject any path traversal even though the
            # sender-side allowlist should never produce one.
            name = Path(info.filename).name
            if not name or name != info.filename:
                log.warning("[ModelTransfer] skipped suspicious entry: %r", info.filename)
                continue
            target = data_dir / name
            target.write_bytes(zf.read(info))
            written.append(name)
    log.info("[ModelTransfer] wrote %d model file(s) to %s", len(written), data_dir)
    return written


def data_dir() -> Path:
    from backend.src.config import DATA_DIR
    return Path(DATA_DIR)
