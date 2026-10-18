"""Run the registered entry trials on explicit offline UTC opportunities/quotes.

No database, broker, training, settings or automatic deployment access.
"""
from __future__ import annotations
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path

from backend.src.services.backtest import entry_trials


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="JSON containing opportunities and quotes")
    parser.add_argument("--output", type=Path, required=True, help="new research JSON artifact")
    parser.add_argument("--protocol-only", action="store_true")
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("output already exists; choose a new artifact path")
    if args.protocol_only:
        protocol = asdict(entry_trials.Protocol())
        result = {"protocol": protocol, "protocol_hash": entry_trials.identity(protocol),
                  "families": entry_trials.FAMILIES, "deployable": False}
    else:
        if args.input is None:
            parser.error("--input is required unless --protocol-only")
        data = json.loads(args.input.read_text(encoding="utf-8"))
        result = entry_trials.run(data["opportunities"], data["quotes"])
    result["source_hash"] = hashlib.sha256(Path(entry_trials.__file__).read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
