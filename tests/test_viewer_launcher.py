import json
from pathlib import Path

from hcs_ai.visualization.launcher import launch_scene
from hcs_ai.visualization.scene import Scene, SceneNode


def test_launch_scene_starts_viewer_process_with_serialized_scene(monkeypatch):
    calls = []

    class DummyProcess:
        pass

    def fake_popen(args, **kwargs):
        calls.append((args, kwargs))
        return DummyProcess()

    monkeypatch.setattr('hcs_ai.visualization.launcher.subprocess.Popen', fake_popen)
    process = launch_scene(Scene(nodes=(SceneNode('n', 'sphere'),)), title='Tree Viewer')
    assert isinstance(process, DummyProcess)
    args, kwargs = calls[0]
    assert args[1:3] == ['-m', 'hcs_ai.visualization.viewer_cli']
    assert '--title' in args and 'Tree Viewer' in args
    scene_path = Path(args[args.index('--scene') + 1])
    data = json.loads(scene_path.read_text(encoding='utf-8'))
    assert data['nodes'][0]['node_id'] == 'n'
    scene_path.unlink()
