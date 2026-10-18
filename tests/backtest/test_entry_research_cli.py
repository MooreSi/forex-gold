"""Offline artifact contract; no app or broker initialisation."""
import json
import pytest
from tools.signal_entry_research import main


def test_protocol_artifact_has_identity_and_cannot_overwrite(tmp_path):
    output = tmp_path / "protocol.json"
    assert main(["--protocol-only", "--output", str(output)]) == 0
    result = json.loads(output.read_text())
    assert len(result["source_hash"]) == 64
    assert len(result["protocol_hash"]) == 64
    assert result["deployable"] is False
    with pytest.raises(SystemExit):
        main(["--protocol-only", "--output", str(output)])


def test_empty_dataset_is_ineligible_for_every_family(tmp_path):
    source, output = tmp_path / "in.json", tmp_path / "out.json"
    source.write_text(json.dumps({"opportunities": [], "quotes": []}))
    assert main(["--input", str(source), "--output", str(output)]) == 0
    result = json.loads(output.read_text())
    assert len(result["trials"]) == 3
    assert all(not t["summary"]["review_eligible"] for t in result["trials"].values())
