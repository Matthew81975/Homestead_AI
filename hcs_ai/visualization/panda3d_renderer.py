from __future__ import annotations

import importlib.util
import math

from .scene import Scene, SceneNode


class Panda3DUnavailable(RuntimeError):
    pass


def panda3d_available() -> bool:
    return importlib.util.find_spec('panda3d') is not None and importlib.util.find_spec('direct') is not None


def scene_to_panda_position(value: tuple[float, float, float]) -> tuple[float, float, float]:
    """Convert HCS scene coordinates (x right, y up, z depth) to Panda3D (x right, y depth, z up)."""
    x, y, z = value
    return (x, z, y)


def scene_to_panda_scale(value: tuple[float, float, float]) -> tuple[float, float, float]:
    x, y, z = value
    return (x, z, y)


class Panda3DViewer:
    """Small shared HCS viewer for renderer-neutral Scene objects.

    Panda3D is imported only when this class is instantiated. This keeps the
    main HCS process usable on installations that have not installed the
    optional visualization dependency yet.
    """

    def __init__(self, title: str = 'HCS 3D Viewer') -> None:
        if not panda3d_available():
            raise Panda3DUnavailable(
                'Panda3D is not installed. Install the optional HCS visualization requirements.'
            )

        from direct.showbase.ShowBase import ShowBase
        from panda3d.core import WindowProperties

        self.base = ShowBase()
        props = WindowProperties()
        props.setTitle(title)
        self.base.win.requestProperties(props)
        self._scene_root = None

    def load_scene(self, scene: Scene) -> None:
        from panda3d.core import LineSegs, TransparencyAttrib

        if self._scene_root is not None:
            self._scene_root.removeNode()
        self._scene_root = self.base.render.attachNewNode('hcs-scene')

        for node in scene.nodes:
            path = self._make_node(node)
            if path is None:
                continue
            path.reparentTo(self._scene_root)
            path.setTag('hcs_node_id', node.node_id)
            if node.primitive == 'void_box':
                path.setRenderModeWireframe()
                path.setColor(1.0, 0.55, 0.2, 0.8)
                path.setTransparency(TransparencyAttrib.MAlpha)
            elif node.primitive == 'analysis_box':
                path.setColor(1.0, 0.2, 0.2, 0.28)
                path.setTransparency(TransparencyAttrib.MAlpha)
            elif node.layer == 'knowledge':
                path.setColor(0.35, 0.65, 1.0, 1.0)
            else:
                path.setColor(0.72, 0.72, 0.72, 1.0)

        if scene.edges:
            lines = LineSegs('hcs-scene-edges')
            lines.setThickness(1.5)
            lines.setColor(0.55, 0.55, 0.55, 1.0)
            for edge in scene.edges:
                source = scene.get_node(edge.source_id)
                target = scene.get_node(edge.target_id)
                lines.moveTo(*scene_to_panda_position(source.transform.position))
                lines.drawTo(*scene_to_panda_position(target.transform.position))
            edge_path = self._scene_root.attachNewNode(lines.create())
            edge_path.setTag('hcs_layer', 'edges')

        self._fit_camera(scene)

    def run(self) -> None:
        self.base.run()

    def _make_node(self, node: SceneNode):
        model_name = 'models/misc/sphere' if node.primitive == 'sphere' else 'models/box'
        model = self.base.loader.loadModel(model_name)
        if model is None:
            return None

        holder = self.base.render.attachNewNode(f'hcs:{node.node_id}')
        model.reparentTo(holder)
        if node.primitive != 'sphere':
            model.setPos(-0.5, -0.5, -0.5)

        holder.setPos(*scene_to_panda_position(node.transform.position))
        holder.setHpr(node.transform.rotation[1], node.transform.rotation[2], node.transform.rotation[0])
        holder.setScale(*scene_to_panda_scale(node.transform.scale))
        if not node.visible:
            holder.hide()
        return holder

    def _fit_camera(self, scene: Scene) -> None:
        if not scene.nodes:
            return
        points = [scene_to_panda_position(node.transform.position) for node in scene.nodes]
        cx = sum(point[0] for point in points) / len(points)
        cy = sum(point[1] for point in points) / len(points)
        cz = sum(point[2] for point in points) / len(points)
        radius = max(
            math.dist((cx, cy, cz), point) + max(scene_to_panda_scale(node.transform.scale)) / 2.0
            for point, node in zip(points, scene.nodes)
        )
        distance = max(8.0, radius * 3.0)
        self.base.camera.setPos(cx, cy - distance, cz + distance * 0.25)
        self.base.camera.lookAt(cx, cy, cz)
