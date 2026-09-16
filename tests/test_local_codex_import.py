from pathlib import Path

from hcs_ai.local_codex import VERSION
from hcs_ai.local_codex.runtime import run_task


def test_local_codex_runtime_is_native_hcs_package():
    assert VERSION == "2.10.8"
    assert run_task.__module__ == "hcs_ai.local_codex.runtime"
    assert Path(__import__("hcs_ai.local_codex.tornado", fromlist=["x"]).__file__).parts[-3:-1] == (
        "hcs_ai", "local_codex"
    )
