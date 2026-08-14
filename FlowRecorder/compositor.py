import subprocess
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

    @staticmethod
    def output_size(monitor, mode):
        mw, mh = int(monitor["width"]), int(monitor["height"])
        if mode == "vertical":
            w, h = min(mw, int(mh * 9 / 16)), mh
        else:
            w, h = mw, min(mh, int(mw * 9 / 16))
        return max(2, w - w % 2), max(2, h - h % 2)

    def _screen(self):
        frame = np.asarray(self.sct.grab(self.monitor), dtype=np.uint8)
        return cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)

    def _camera(self):
        if self.camera is None:
            self.camera = cv2.VideoCapture(self.webcam_index, cv2.CAP_DSHOW)
            self.camera.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
            self.camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        ok, frame = self.camera.read()
        return frame if ok else None

    @staticmethod
    def _fit(frame, width, height):
        if frame is None or width <= 1 or height <= 1:
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
        return canvas

    def close(self):
        try: self.sct.close()
        except Exception: pass
        if self.camera is not None:
            try: self.camera.release()
            except Exception: pass


class CompositeRecorderMixin:
    """Replace Recorder's raw screen video worker with scene composition."""
    def video(self):
        compositor = SceneCompositor(self.scene, self.monitor, self.mode, self.fps)
        width, height = compositor.output_size(self.monitor, self.mode)
        ffmpeg = self.ffmpeg() if callable(getattr(self, "ffmpeg", None)) else "ffmpeg"
        cmd = [ffmpeg, "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
               "-video_size", f"{width}x{height}", "-framerate", str(self.fps),
               "-i", "-", "-an", "-c:v", "libx264", "-preset", "veryfast",
               "-crf", "23", "-pix_fmt", "yuv420p", str(self.video_file)]
        proc = None
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            interval = 1.0 / self.fps
            next_frame = time.perf_counter()
            while not self.stop_event.is_set():
                proc.stdin.write(compositor.render().tobytes())
                next_frame += interval
                delay = next_frame - time.perf_counter()
                if delay > 0: time.sleep(delay)
                elif delay < -interval * 2: next_frame = time.perf_counter()
            proc.stdin.close()
            err = proc.stderr.read().decode(errors="replace")
            if proc.wait(): self.errors.append("FFmpeg composición: " + (err[-2500:] or "error desconocido"))
        except Exception as exc:
            self.errors.append("Composición: " + str(exc))
            if proc:
                try: proc.kill()
                except Exception: pass
        finally:
            compositor.close()


def make_composite_recorder(base_recorder_cls):
    return type("CompositeRecorder", (CompositeRecorderMixin, base_recorder_cls), {})
