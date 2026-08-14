import time
from pathlib import Path

import cv2
import numpy as np
from mss import mss


class SceneCompositor:
    """Renders the active FlowRecorder scene into video frames."""

    def __init__(self, scene, monitor, mode="horizontal", fps=30, webcam_index=0):
        self.scene = scene
        self.monitor = monitor
        self.mode = mode
        self.fps = fps
        self.webcam_index = webcam_index
        self.sct = mss()
        self.camera = None
        self.last_frame = None

    @staticmethod
    def output_size(monitor, mode):
        w, h = int(monitor["width"]), int(monitor["height"])
        if mode == "vertical":
            w = min(w, int(h * 9 / 16))
            w -= w % 2
            h -= h % 2
        else:
            h = min(h, int(w * 9 / 16))
            h -= h % 2
            w -= w % 2
        return max(2, w), max(2, h)

    def _screen(self):
        frame = np.asarray(self.sct.grab(self.monitor), dtype=np.uint8)
        return cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)

    def _camera(self):
        if self.camera is None:
            self.camera = cv2.VideoCapture(self.webcam_index, cv2.CAP_DSHOW)
            self.camera.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
            self.camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        ok, frame = self.camera.read()
        if not ok:
            return None
        return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)[:, :, ::-1]

    @staticmethod
    def _fit(frame, width, height):
        if frame is None:
            return None
        h, w = frame.shape[:2]
        if w <= 0 or h <= 0:
            return None
        scale = max(width / w, height / h)
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
        resized = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA)
        x, y = max(0, (nw - width) // 2), max(0, (nh - height) // 2)
        return resized[y:y + height, x:x + width]

    def _draw_source(self, canvas, source):
        x = max(0, min(canvas.shape[1] - 1, int(source.x * canvas.shape[1])))
        y = max(0, min(canvas.shape[0] - 1, int(source.y * canvas.shape[0])))
        w = max(2, min(canvas.shape[1] - x, int(source.width * canvas.shape[1])))
        h = max(2, min(canvas.shape[0] - y, int(source.height * canvas.shape[0])))

        kind = source.kind.lower()
        frame = None
        if kind == "screen":
            frame = self._fit(self._screen(), w, h)
        elif kind == "camera":
            frame = self._fit(self._camera(), w, h)
        elif kind == "image":
            path = getattr(source, "path", "")
            if path and Path(path).exists():
                frame = self._fit(cv2.imread(path), w, h)
        elif kind == "video":
            path = getattr(source, "path", "")
            if path and Path(path).exists():
                if not hasattr(source, "_capture"):
                    source._capture = cv2.VideoCapture(path)
                ok, frame = source._capture.read()
                if not ok:
                    source._capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    ok, frame = source._capture.read()
                frame = self._fit(frame, w, h) if ok else None
        elif kind == "text":
            frame = np.zeros((h, w, 3), dtype=np.uint8)
            cv2.putText(frame, source.name, (18, min(h - 12, 54)), cv2.FONT_HERSHEY_SIMPLEX,
                        max(.5, min(2.0, w / 500)), (245, 245, 245), 2, cv2.LINE_AA)

        if frame is not None:
            canvas[y:y + h, x:x + w] = frame
        else:
            cv2.rectangle(canvas, (x, y), (x + w - 1, y + h - 1), (60, 72, 88), 1)
            cv2.putText(canvas, source.name, (x + 10, min(y + 28, canvas.shape[0] - 8)),
                        cv2.FONT_HERSHEY_SIMPLEX, .65, (180, 190, 205), 1, cv2.LINE_AA)

    def render(self):
        width, height = self.output_size(self.monitor, self.mode)
        canvas = np.zeros((height, width, 3), dtype=np.uint8)
        for source in self.scene.sources:
            if source.enabled:
                self._draw_source(canvas, source)
        self.last_frame = canvas
        return canvas

    def close(self):
        try:
            self.sct.close()
        except Exception:
            pass
        if self.camera is not None:
            try:
                self.camera.release()
            except Exception:
                pass
        for source in self.scene.sources:
            capture = getattr(source, "_capture", None)
            if capture is not None:
                try:
                    capture.release()
                except Exception:
                    pass
