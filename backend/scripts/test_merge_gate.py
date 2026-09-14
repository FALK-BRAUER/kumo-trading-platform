"""merge_gate refuses when it cannot verify. The scaffold body exits 2 on every call."""
import subprocess
import sys
from pathlib import Path


def test_scaffold_gate_refuses_rather_than_passing():
    gate = Path(__file__).with_name("merge_gate.py")
    proc = subprocess.run([sys.executable, str(gate), "1"], capture_output=True, text=True)
    assert proc.returncode != 0
    assert "not implemented" in proc.stderr
