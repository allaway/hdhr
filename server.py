#!/usr/bin/env python3
"""Tiny HDHomeRun -> browser bridge.

Discovers an HDHomeRun on the LAN, serves a channel list, and transcodes
the selected channel (MPEG-2/AC3 broadcast) to HLS (H.264/AAC) with ffmpeg
so any browser can play it. Standard library only; needs ffmpeg on PATH.

    python3 server.py [--port 8080] [--device http://<hdhomerun-ip>]
"""
import argparse
import json
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORK = Path(tempfile.mkdtemp(prefix="hdhr-web-"))
IDLE_TIMEOUT = 30  # seconds without a playlist request before ffmpeg is stopped
CHANNEL_RE = re.compile(r"^[0-9]+(\.[0-9]+)?$")


def fetch_json(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def discover():
    """Find a device via SiliconDust's cloud discovery (same as their apps)."""
    try:
        devices = fetch_json("https://ipv4-api.hdhomerun.com/discover")
        for d in devices:
            if "LineupURL" in d:
                return d["BaseURL"]
    except Exception as e:
        print(f"discovery failed: {e}", file=sys.stderr)
    return None


def pick_encoder():
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                         capture_output=True, text=True).stdout
    if "h264_videotoolbox" in out:
        return ["-c:v", "h264_videotoolbox", "-realtime", "1", "-b:v", "10M",
                "-maxrate", "20M", "-profile:v", "high"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
            "-crf", "22", "-maxrate", "20M", "-bufsize", "40M"]


class Stream:
    def __init__(self, channel, url, encoder):
        self.channel = channel
        self.dir = WORK / channel
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir(parents=True)
        self.last_access = self.started = time.time()
        self.stopped = False
        self.log = open(self.dir / "ffmpeg.log", "w")
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            "-fflags", "+genpts+discardcorrupt",
            "-i", url,
            "-map", "0:v:0", "-map", "0:a:0?",
            "-vf", "yadif=mode=0:deint=interlaced",
            *encoder,
            "-g", "60", "-force_key_frames", "expr:gte(t,n_forced*2)",
            # Fill gaps from dropped packets (e.g. Wi-Fi hiccups) with silence
            # so audio timestamps stay continuous and playback doesn't stutter.
            "-af", "aresample=async=1000:first_pts=0",
            "-c:a", "aac", "-b:a", "160k", "-ac", "2",
            "-f", "hls", "-hls_time", "2", "-hls_list_size", "10",
            "-hls_flags", "delete_segments+independent_segments+omit_endlist",
            "-hls_segment_filename", str(self.dir / "seg%05d.ts"),
            str(self.dir / "index.m3u8"),
        ]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=self.log)

    @property
    def alive(self):
        return self.proc.poll() is None

    def ready(self):
        pl = self.dir / "index.m3u8"
        return pl.exists() and pl.read_text().count("#EXTINF") >= 2

    def error_text(self):
        try:
            return (self.dir / "ffmpeg.log").read_text()[-500:].strip()
        except OSError:
            return ""

    def report(self, why):
        """Log an unexpected ffmpeg exit to the console (the work dir is deleted)."""
        print(f"[{time.strftime('%H:%M:%S')}] ch {self.channel}: ffmpeg {why}, "
              f"exit code {self.proc.returncode}, ran {time.time() - self.started:.0f}s; "
              f"log: {self.error_text() or '(empty)'}", flush=True)

    def stop(self):
        self.stopped = True
        if self.alive:
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.log.close()
        shutil.rmtree(self.dir, ignore_errors=True)


class Manager:
    def __init__(self, device):
        self.device = device.rstrip("/")
        info = fetch_json(self.device + "/discover.json")
        self.info = info
        self.max_streams = int(info.get("TunerCount", 1))
        self.encoder = pick_encoder()
        self.streams = {}
        self.lock = threading.Lock()
        self._lineup = None
        self._guide_cache = {}
        threading.Thread(target=self._reaper, daemon=True).start()

    def lineup(self, refresh=False):
        if self._lineup is None or refresh:
            self._lineup = fetch_json(self.info["LineupURL"])
        return self._lineup

    def guide(self, start=None):
        """Program guide from SiliconDust's API (what their own app uses).

        Each call covers roughly the next several hours from `start`
        (unix time). Cached per 30-minute slot for 10 minutes.
        """
        key = int(start or 0) // 1800
        hit = self._guide_cache.get(key)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
        # DeviceAuth rotates, so read a fresh one from the tuner each time.
        auth = fetch_json(self.device + "/discover.json")["DeviceAuth"]
        url = ("https://api.hdhomerun.com/api/guide.php?DeviceAuth="
               + urllib.parse.quote(auth))
        if start:
            url += f"&Start={int(start)}"
        data = fetch_json(url, timeout=15)
        self._guide_cache[key] = (time.time(), data)
        if len(self._guide_cache) > 50:  # drop stale entries
            cutoff = time.time() - 600
            self._guide_cache = {k: v for k, v in self._guide_cache.items()
                                 if v[0] > cutoff}
        return data

    def channel_url(self, channel):
        for c in self.lineup():
            if c["GuideNumber"] == channel:
                return c["URL"]
        return None

    def get(self, channel):
        """Return a running Stream for channel, starting one if needed."""
        with self.lock:
            s = self.streams.get(channel)
            if s and not s.alive:
                if not s.stopped:
                    s.report("exited mid-stream")
                s.stop()
                del self.streams[channel]
                s = None
            if s is None:
                url = self.channel_url(channel)
                if url is None:
                    return None
                # Free a tuner if we're at the limit (drop least-recently used).
                while len(self.streams) >= self.max_streams:
                    old = min(self.streams.values(), key=lambda x: x.last_access)
                    old.stop()
                    del self.streams[old.channel]
                s = Stream(channel, url, self.encoder)
                self.streams[channel] = s
            s.last_access = time.time()
            return s

    def touch(self, channel):
        s = self.streams.get(channel)
        if s:
            s.last_access = time.time()
        return s

    def stop(self, channel):
        with self.lock:
            s = self.streams.pop(channel, None)
        if s:
            s.stop()

    def stop_all(self):
        with self.lock:
            for s in self.streams.values():
                s.stop()
            self.streams.clear()

    def _reaper(self):
        while True:
            time.sleep(5)
            now = time.time()
            with self.lock:
                for ch, s in list(self.streams.items()):
                    if now - s.last_access > IDLE_TIMEOUT or not s.alive:
                        if not s.alive:
                            s.report("exited mid-stream")
                        s.stop()
                        del self.streams[ch]


class Handler(SimpleHTTPRequestHandler):
    mgr: "Manager"

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(HERE / "static"), **kw)

    def log_message(self, format, *args):
        if "/hls/" not in self.path:  # keep the console quiet
            super().log_message(format, *args)

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, ctype):
        try:
            data = path.read_bytes()
        except OSError:
            return self.send_error(404)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/info":
            info = {k: v for k, v in self.mgr.info.items() if k != "DeviceAuth"}
            return self.send_json(info | {"Active": list(self.mgr.streams)})
        if path == "/api/channels":
            return self.send_json(self.mgr.lineup(refresh="refresh" in self.path))
        if path == "/api/status":
            try:
                tuners = fetch_json(self.mgr.device + "/status.json", timeout=3)
            except Exception as e:
                tuners = {"error": str(e)}
            return self.send_json({"tuners": tuners, "encoder": self.mgr.encoder,
                                   "active": list(self.mgr.streams)})
        if path == "/api/guide":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            start = qs.get("start", [None])[0]
            try:
                start = int(start) if start else None
                return self.send_json(self.mgr.guide(start))
            except ValueError:
                return self.send_json({"error": "bad start"}, 400)
            except Exception as e:
                return self.send_json({"error": f"guide unavailable: {e}"}, 502)
        m = re.match(r"^/hls/([^/]+)/(index\.m3u8|seg\d+\.ts)$", path)
        if m:
            return self.serve_hls(m.group(1), m.group(2))
        return super().do_GET()

    def do_POST(self):
        m = re.match(r"^/api/stop/([^/]+)$", self.path)
        if m:
            self.mgr.stop(m.group(1))
            return self.send_json({"ok": True})
        self.send_error(404)

    def serve_hls(self, channel, name):
        if not CHANNEL_RE.match(channel):
            return self.send_error(400)
        if name.endswith(".ts"):
            s = self.mgr.touch(channel)
            if not s:
                return self.send_error(404)
            return self.send_file(s.dir / name, "video/mp2t")

        s = self.mgr.get(channel)
        if s is None:
            return self.send_json({"error": "unknown channel"}, 404)
        deadline = time.time() + 25
        while not s.ready():
            if not s.alive and s.stopped:
                # Stopped on purpose by a concurrent request (e.g. a stop
                # beacon racing a retune): just start it again.
                if time.time() > deadline:
                    return self.send_json({"error": "timed out tuning channel"}, 504)
                s = self.mgr.get(channel)
                assert s is not None
                time.sleep(0.25)
                continue
            if not s.alive:
                msg = s.error_text() or f"ffmpeg exited (code {s.proc.returncode})"
                s.report("failed while tuning")
                self.mgr.stop(channel)
                if "503" in msg:
                    # Tuners busy. If we hold another one, give it up and retry
                    # (another device on the LAN may be using the rest).
                    others = [x for x in self.mgr.streams.values() if x.channel != channel]
                    if others:
                        self.mgr.stop(min(others, key=lambda x: x.last_access).channel)
                        s = self.mgr.get(channel)
                        assert s is not None  # channel was valid a moment ago
                        time.sleep(0.5)
                        continue
                    msg = "No free tuner (other devices are using them all)."
                return self.send_json({"error": msg}, 503)
            if time.time() > deadline:
                return self.send_json({"error": "timed out tuning channel"}, 504)
            time.sleep(0.25)
        self.send_file(s.dir / "index.m3u8", "application/vnd.apple.mpegurl")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1",
                    help="use 0.0.0.0 to allow other devices on your LAN")
    ap.add_argument("--device", help="HDHomeRun base URL, e.g. http://<hdhomerun-ip>")
    args = ap.parse_args()

    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found on PATH (brew install ffmpeg)")
    device = args.device or discover()
    if not device:
        sys.exit("No HDHomeRun found; pass --device http://<ip>")

    Handler.mgr = mgr = Manager(device)
    print(f"{mgr.info.get('FriendlyName')} at {device} "
          f"({mgr.max_streams} tuners, encoder: {mgr.encoder[1]})")
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True

    def _quit(*_):
        raise KeyboardInterrupt
    # Shut down cleanly (stopping ffmpeg, freeing tuners) on Ctrl-C or kill,
    # even when launched in the background where SIGINT is ignored.
    signal.signal(signal.SIGINT, _quit)
    signal.signal(signal.SIGTERM, _quit)
    print(f"Open http://{'localhost' if args.host == '127.0.0.1' else args.host}:{args.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        mgr.stop_all()
        shutil.rmtree(WORK, ignore_errors=True)


if __name__ == "__main__":
    main()
