from __future__ import annotations

import argparse
import json
from pathlib import Path

from .panda3d_renderer import Panda3DViewer
from .scene_io import scene_from_dict


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='HCS shared 3D scene viewer')
    parser.add_argument('--scene', required=True, help='Path to serialized HCS scene JSON')
    parser.add_argument('--title', default='HCS 3D Viewer')
    args = parser.parse_args(argv)

    scene_path = Path(args.scene)
    try:
        data = json.loads(scene_path.read_text(encoding='utf-8'))
        scene = scene_from_dict(data)
    finally:
        scene_path.unlink(missing_ok=True)

    viewer = Panda3DViewer(title=args.title)
    viewer.load_scene(scene)
    viewer.run()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
