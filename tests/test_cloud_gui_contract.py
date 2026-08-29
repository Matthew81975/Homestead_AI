from pathlib import Path


GUI = Path("hcs_ai/gui.py").read_text(encoding="utf-8")


def test_gui_sends_stable_cloud_task_id():
    assert '"task_id": self.cloud_task_id' in GUI


def test_gui_displays_cloud_route_metadata():
    assert "Cloud:" in GUI
    assert "cloud_route_label" in GUI


def test_gui_has_cross_tier_approval_call():
    assert '"/ai/approve-tier"' in GUI
    assert "askyesno" in GUI
