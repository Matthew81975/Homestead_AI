from pathlib import Path

from hcs_ai.desktop_host import server_python_executable


def test_pythonw_host_uses_sibling_python_for_server(tmp_path):
    pythonw = tmp_path / "pythonw.exe"
    python = tmp_path / "python.exe"
    pythonw.write_text("", encoding="utf-8")
    python.write_text("", encoding="utf-8")

    assert server_python_executable(str(pythonw)) == str(python)


def test_console_python_host_keeps_current_interpreter(tmp_path):
    python = tmp_path / "python.exe"
    python.write_text("", encoding="utf-8")

    assert server_python_executable(str(python)) == str(python)


def test_pythonw_falls_back_if_sibling_python_is_missing(tmp_path):
    pythonw = tmp_path / "pythonw.exe"
    pythonw.write_text("", encoding="utf-8")

    assert server_python_executable(str(pythonw)) == str(pythonw)
