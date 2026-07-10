"""Local camera presence sensor for Eva ("eyes").

A separate worker process reads the webcam, detects whether a person is present
(OpenCV Haar cascade face detection + frame-difference motion), and writes the
latest downscaled JPEG frame plus a small JSON state to a spool directory. The
bridge process (parent) only reads those files, so a flaky V4L2 driver can never
freeze the bridge: stopping the sensor simply kills the worker process, which
releases the camera regardless of driver state.

Why a subprocess and not a thread: on some V4L2 devices cv2.VideoCapture.release
wedges (REQBUFS errno=19) holding the GIL, which would freeze the whole bridge.
A child process is killable instantly and the OS reclaims the camera fd.

Design notes:
  - Local CV only here (no network). The cloud "look" happens in the frontend,
    which fetches a frame and posts it to the vision model. Frames stay off the
    network unless a look is explicitly requested.
  - cv2 and numpy are imported lazily (only in the worker) so a missing install
    never breaks bridge import. mediapipe is intentionally not required: on newer
    Pythons it ships only the Tasks API (no legacy mp.solutions), so Haar cascade
    is the dependable path for v1 presence.

PRIVACY: the camera is OFF by default. It only opens on an explicit start() and
is released when the worker is stopped. One worker at a time.
"""

import os
import sys
import json
import time
import signal
import threading
import subprocess

from bridge import config as _bridge_config

_CAM_DIR = os.path.expanduser("~/.config/eva-standalone/camera")
_STATE_PATH = os.path.join(_CAM_DIR, "state.json")
_FRAME_PATH = os.path.join(_CAM_DIR, "frame.jpg")

_DEFAULT_DEVICE = int(os.environ.get("EVA_CAMERA_DEVICE", "0"))
_TARGET_FPS = 5
_CAP_WIDTH = 640
_CAP_HEIGHT = 480
_JPEG_QUALITY = 80
_PRESENT_ON_FRAMES = 2      # face seen N samples before flipping present True
_PRESENT_OFF_FRAMES = 8     # slower to drop (tolerate brief look-aways)

# Parent-side process handle.
_proc_lock = threading.Lock()
_proc = None
_proc_device = None


def opencv_available():
    """Return (ok, detail). True when cv2 + numpy import."""
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
        return True, "ok"
    except Exception as e:
        return False, f"OpenCV/numpy not available: {e}"


# ---------------------------------------------------------------------------
# Parent API (used by the bridge)
# ---------------------------------------------------------------------------

def _atomic_write_bytes(path, data):
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def start(device=None):
    """Spawn (or restart on a new device) the capture worker. Returns status."""
    ok, detail = opencv_available()
    if not ok:
        raise RuntimeError(detail)
    dev = _DEFAULT_DEVICE if device is None else int(device)
    if dev < 0 or dev > 64:
        raise RuntimeError("invalid camera device index")
    os.makedirs(_CAM_DIR, exist_ok=True)

    global _proc, _proc_device
    with _proc_lock:
        if _proc is not None and _proc.poll() is None:
            if _proc_device == dev:
                return _read_status()
            _stop_locked()  # different device requested: restart

        # Seed an initial state so the frontend sees "enabled" immediately.
        try:
            _atomic_write_bytes(_STATE_PATH, json.dumps({
                "enabled": True, "device": dev, "present": False, "looking": False,
                "faces": 0, "motion": 0.0, "arrival_seq": 0, "has_frame": False,
                "error": None, "ts": time.time(),
            }).encode("utf-8"))
        except Exception:
            pass
        try:
            os.remove(_FRAME_PATH)
        except OSError:
            pass

        _proc = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__),
             "--worker", "--device", str(dev), "--dir", _CAM_DIR],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=_bridge_config.child_process_env(),
        )
        _proc_device = dev
        return _read_status()


def _stop_locked():
    global _proc, _proc_device
    p = _proc
    _proc = None
    _proc_device = None
    if p is None:
        return
    if p.poll() is None:
        try:
            p.terminate()
        except Exception:
            pass
        try:
            p.wait(timeout=1.5)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    try:
        _atomic_write_bytes(_STATE_PATH, json.dumps({
            "enabled": False, "present": False, "looking": False, "faces": 0,
            "motion": 0.0, "arrival_seq": 0, "has_frame": False, "error": None,
            "ts": time.time(),
        }).encode("utf-8"))
    except Exception:
        pass
    # Delete the last frame so a subsequent read can never serve a stale image
    # from a previous session before the next worker captures a fresh one.
    try:
        os.remove(_FRAME_PATH)
    except OSError:
        pass


def stop():
    """Kill the worker and release the camera. Returns a disabled status."""
    with _proc_lock:
        _stop_locked()
    return status()


def _read_status():
    try:
        with open(_STATE_PATH, "r") as f:
            st = json.load(f)
    except Exception:
        st = {"enabled": False, "present": False, "looking": False, "faces": 0,
              "motion": 0.0, "arrival_seq": 0, "has_frame": False, "error": None}
    alive = bool(_proc is not None and _proc.poll() is None)
    st["enabled"] = alive and st.get("enabled", False)
    # Derive state age from the JSON timestamp so callers can spot a stalled worker.
    ts = st.get("ts")
    if ts:
        st["state_age_ms"] = int((time.time() - float(ts)) * 1000)
    return st


def status():
    with _proc_lock:
        return _read_status()


def latest_jpeg():
    """Return the latest JPEG frame bytes, or None."""
    with _proc_lock:
        if _proc is None or _proc.poll() is not None:
            return None
    try:
        with open(_FRAME_PATH, "rb") as f:
            return f.read()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Worker process (runs the actual capture loop in isolation)
# ---------------------------------------------------------------------------

def _worker_main(device, spool_dir):
    import cv2
    import numpy as np

    state_path = os.path.join(spool_dir, "state.json")
    frame_path = os.path.join(spool_dir, "frame.jpg")

    stop_flag = {"stop": False}

    def _on_term(_sig, _frm):
        stop_flag["stop"] = True
    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)

    def write_state(d):
        try:
            tmp = state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(d, f)
            os.replace(tmp, state_path)
        except Exception:
            pass

    def write_frame(buf):
        try:
            tmp = frame_path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(buf)
            os.replace(tmp, frame_path)
        except Exception:
            pass

    def _open_capture(preferred):
        """Open a camera that actually yields a frame.

        The requested index is tried first, then other present /dev/video*
        indices. Cameras renumber across reboots/replugs (e.g. video0 -> video1),
        so a hardcoded index silently produces empty frames. An index counts as
        working only if it opens AND reads a non-None frame.
        """
        candidates = [preferred]
        try:
            import glob
            for path in sorted(glob.glob("/dev/video*")):
                tail = path.replace("/dev/video", "")
                if tail.isdigit():
                    idx = int(tail)
                    if idx not in candidates:
                        candidates.append(idx)
        except Exception:
            pass
        # Fall back to a small numeric sweep if globbing found nothing.
        for idx in range(0, 6):
            if idx not in candidates:
                candidates.append(idx)

        for idx in candidates:
            try:
                c = cv2.VideoCapture(idx)
            except Exception:
                continue
            if not c.isOpened():
                try:
                    c.release()
                except Exception:
                    pass
                continue
            ok, frame = c.read()
            if ok and frame is not None:
                return c, idx
            try:
                c.release()
            except Exception:
                pass
        return None, None

    cap, device = _open_capture(device)
    if cap is None:
        write_state({"enabled": False, "device": device, "present": False,
                     "looking": False, "faces": 0, "motion": 0.0, "arrival_seq": 0,
                     "has_frame": False, "error": "no working camera device found",
                     "ts": time.time()})
        return
    try:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, _CAP_WIDTH)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, _CAP_HEIGHT)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except Exception:
        pass

    clf = cv2.CascadeClassifier(
        os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml"))
    if clf.empty():
        write_state({"enabled": False, "device": device, "present": False,
                     "looking": False, "faces": 0, "motion": 0.0, "arrival_seq": 0,
                     "has_frame": False, "error": "face cascade failed to load",
                     "ts": time.time()})
        cap.release()
        return

    prev_gray = None
    present = False
    on_streak = 0
    off_streak = 0
    arrival_seq = 0
    frame_seq = 0
    last_seen = 0.0
    present_since = 0.0
    interval = 1.0 / float(_TARGET_FPS)
    has_frame = False

    # Flush warm-up frames: many V4L2 webcams deliver several stale buffered
    # frames immediately after open (the "stale/cached image" on a fresh look).
    # Discard a handful so the first frame we publish is genuinely live.
    for _ in range(5):
        try:
            cap.read()
        except Exception:
            break

    while not stop_flag["stop"]:
        t0 = time.time()
        ok, frame = cap.read()
        if not ok or frame is None:
            time.sleep(interval)
            continue

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        motion = 0.0
        if prev_gray is not None and prev_gray.shape == gray.shape:
            motion = float(np.mean(cv2.absdiff(gray, prev_gray)))
        prev_gray = gray

        faces = clf.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5,
                                     minSize=(60, 60))
        n_faces = len(faces)
        face_now = n_faces > 0
        now = time.time()
        if face_now:
            on_streak += 1
            off_streak = 0
            last_seen = now
        else:
            off_streak += 1
            on_streak = 0

        if not present and on_streak >= _PRESENT_ON_FRAMES:
            present = True
            present_since = now
            arrival_seq += 1
        elif present and off_streak >= _PRESENT_OFF_FRAMES:
            present = False
            present_since = 0.0

        try:
            enc_ok, buf = cv2.imencode(".jpg", frame,
                                       [cv2.IMWRITE_JPEG_QUALITY, _JPEG_QUALITY])
            if enc_ok:
                write_frame(buf.tobytes())
                has_frame = True
                frame_seq += 1
        except Exception:
            pass

        write_state({
            "enabled": True, "device": device, "present": present,
            "looking": face_now, "faces": n_faces, "motion": round(motion, 2),
            "arrival_seq": arrival_seq, "frame_seq": frame_seq,
            "has_frame": has_frame, "error": None,
            "last_seen_ms": int((now - last_seen) * 1000) if last_seen else None,
            "present_for_ms": int((now - present_since) * 1000) if present_since else None,
            "ts": now,
        })

        dt = time.time() - t0
        if dt < interval:
            time.sleep(interval - dt)

    try:
        cap.release()
    except Exception:
        pass
    write_state({"enabled": False, "device": device, "present": False,
                 "looking": False, "faces": 0, "motion": 0.0,
                 "arrival_seq": arrival_seq, "has_frame": False, "error": None,
                 "ts": time.time()})


def _parse_worker_args(argv):
    device = 0
    spool = _CAM_DIR
    i = 0
    while i < len(argv):
        if argv[i] == "--device" and i + 1 < len(argv):
            try:
                device = int(argv[i + 1])
            except ValueError:
                device = 0
            i += 2
        elif argv[i] == "--dir" and i + 1 < len(argv):
            spool = argv[i + 1]
            i += 2
        else:
            i += 1
    return device, spool


if __name__ == "__main__" and "--worker" in sys.argv:
    _dev, _spool = _parse_worker_args(sys.argv[1:])
    try:
        os.makedirs(_spool, exist_ok=True)
    except Exception:
        pass
    _worker_main(_dev, _spool)
