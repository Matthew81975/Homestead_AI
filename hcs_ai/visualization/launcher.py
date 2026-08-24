from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from .scene import Scene
from .scene_io import scene_to_dict


def launch_scene(scene: Scene, title: str = 'HCS 3D Viewer'):
    """Launch a renderer process without blocking HCS's Tkinter event loop."""
    with tempfile.NamedTemporaryFile(
        mode='w', encoding='utf-8', suffix='.hcs-scene.json', delete=False
    ) as handle:
        json.dump(scene_to_dict(scene), handle, ensure_ascii=False)
        scene_path = Path(handle.name)

    args = [
        sys.executable,
        '-m',
        'hcs_ai.visualization.viewer_cli',
        '--scene',
        str(scene_path),
        '--title',
        title,
    ]
    try:
        return subprocess.Popen(args)
    except Exception:
        scene_path.unlink(missing_ok=True)
        raise
