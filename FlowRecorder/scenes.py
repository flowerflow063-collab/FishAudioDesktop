from dataclasses import dataclass, asdict
from typing import List
import json
from pathlib import Path


@dataclass
class Source:
    name: str
    kind: str
    enabled: bool = True
    x: float = 0.0
    y: float = 0.0
    width: float = 1.0
    height: float = 1.0


@dataclass
class Scene:
    name: str
    sources: List[Source]


class SceneManager:
    """Persistent scene/source model used by FlowRecorder's Studio."""
    def __init__(self, path=None):
        self.path = Path(path) if path else Path.home() / "Videos" / "FlowRecorder" / "scenes.json"
        self.scenes: List[Scene] = []
        self.active_index = 0
        self.load()
        if not self.scenes:
            self.scenes = [
                Scene("Escritorio", [Source("Pantalla", "screen")]),
                Scene("Pantalla + cámara", [
                    Source("Pantalla", "screen", True, 0, 0, 1, 1),
                    Source("Cámara", "camera", True, .72, .72, .26, .26),
                ]),
                Scene("Vertical", [Source("Pantalla", "screen")]),
            ]
            self.save()

    @property
    def active(self):
        return self.scenes[self.active_index] if self.scenes else None

    def add_scene(self, name):
        name = name.strip() or "Nueva escena"
        self.scenes.append(Scene(name, []))
        self.active_index = len(self.scenes) - 1
        self.save()
        return self.active

    def remove_scene(self, index):
        if len(self.scenes) <= 1:
            return False
        if 0 <= index < len(self.scenes):
            self.scenes.pop(index)
            self.active_index = min(self.active_index, len(self.scenes) - 1)
            self.save()
            return True
        return False

    def add_source(self, scene_index, source):
        if 0 <= scene_index < len(self.scenes):
            self.scenes[scene_index].sources.append(source)
            self.save()
            return True
        return False

    def remove_source(self, scene_index, source_index):
        if 0 <= scene_index < len(self.scenes):
            sources = self.scenes[scene_index].sources
            if 0 <= source_index < len(sources):
                sources.pop(source_index)
                self.save()
                return True
        return False

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "active_index": self.active_index,
            "scenes": [
                {"name": s.name, "sources": [asdict(x) for x in s.sources]}
                for s in self.scenes
            ],
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def load(self):
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            self.scenes = [
                Scene(s["name"], [Source(**src) for src in s.get("sources", [])])
                for s in payload.get("scenes", [])
            ]
            self.active_index = min(int(payload.get("active_index", 0)), max(0, len(self.scenes) - 1))
        except Exception:
            self.scenes = []
            self.active_index = 0
