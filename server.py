#!/usr/bin/env python3
"""Tiny HDHomeRun -> browser bridge.

Discovers an HDHomeRun on the LAN, serves a channel list, and transcodes
the selected channel (MPEG-2/AC3 broadcast) to HLS (H.264/AAC) with ffmpeg
so any browser can play it. Standard library only; needs ffmpeg on PATH.

    python3 server.py [--port 8080] [--device http://<hdhomerun-ip>]
"""
import argparse
import json
import os
import platform
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
WORK = Path(tempfile.gettempdir())  # replaced in main() with a private subdir
IDLE_TIMEOUT = 30  # seconds without a playlist request before ffmpeg is stopped
VIEWER_GRACE = 8   # a viewer counts as watching if it polled the playlist this recently
STALL_TIMEOUT = 20  # kill a stream that stops producing segments for this long
CHANNEL_RE = re.compile(r"^[0-9]+(\.[0-9]+)?$")


def fetch_json(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def discover_local(timeout=1.5):
    """Find an HDHomeRun with its own LAN broadcast protocol (UDP 65001).

    Works without internet access. Packet: type (u16 BE), payload length
    (u16 BE), TLV payload, CRC-32 (u32 LE) over everything before it.
    """
    import socket
    import struct
    import zlib

    def tlv(tag, value):
        return bytes([tag, len(value)]) + value

    payload = tlv(0x01, struct.pack(">I", 0x00000001))   # device type: tuner
    payload += tlv(0x02, struct.pack(">I", 0xFFFFFFFF))  # device id: any
    pkt = struct.pack(">HH", 0x0002, len(payload)) + payload  # discover request
    pkt += struct.pack("<I", zlib.crc32(pkt) & 0xFFFFFFFF)

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.settimeout(timeout)
        s.sendto(pkt, ("255.255.255.255", 65001))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, (ip, _) = s.recvfrom(2048)
            except OSError:
                break
            if len(data) < 8 or struct.unpack(">H", data[:2])[0] != 0x0003:
                continue  # not a discover reply
            body, pos, tags = data[4:-4], 0, {}
            while pos + 2 <= len(body):
                tag, ln = body[pos], body[pos + 1]
                pos += 2
                if ln & 0x80:  # two-byte length
                    ln = (ln & 0x7F) | (body[pos] << 7)
                    pos += 1
                tags[tag] = body[pos:pos + ln]
                pos += ln
            base = tags.get(0x2A)  # base URL
            return base.decode() if base else f"http://{ip}"
    return None


def discover():
    """Find a device on the LAN first, then via SiliconDust's cloud service."""
    try:
        if url := discover_local():
            return url
    except Exception as e:
        print(f"local discovery failed: {e}", file=sys.stderr)
    try:
        devices = fetch_json("https://ipv4-api.hdhomerun.com/discover")
        for d in devices:
            if "LineupURL" in d:
                return d["BaseURL"]
    except Exception as e:
        print(f"cloud discovery failed: {e}", file=sys.stderr)
    return None


# ── Transcoding profiles ────────────────────────────────────────────────
# Nearly all the CPU goes to deinterlacing and H.264 encoding (measured on
# 20 s of 1080i MPEG-2, one core: decode 0.8 s, yadif 6.2 s, bwdif 1.9 s,
# x264 veryfast 1080p ~21 s). So profiles trade resolution and encoder
# effort; bwdif replaces yadif everywhere (3x cheaper, similar quality).
# deint=interlaced leaves progressive (720p) channels untouched.
PROFILES = {
    #            output height, scaler,          HW bitrate, HW max, x264 preset/crf/max
    "full":     dict(height=None, scaler="bicubic",       br="10M", maxrate="20M",
                     x264=("veryfast", "21", "20M")),
    "balanced": dict(height=720,  scaler="bicubic",       br="6M",  maxrate="10M",
                     x264=("superfast", "22", "8M")),
    "lite":     dict(height=720,  scaler="fast_bilinear", br="4M",  maxrate="6M",
                     x264=("ultrafast", "23", "5M")),
}

VAAPI_DEVICE = "/dev/dri/renderD128"


class Encoder:
    """ffmpeg arguments for one H.264 encoder at one profile."""

    def __init__(self, name, profile):
        self.name, self.profile = name, profile
        p = PROFILES[profile]
        vf = "bwdif=mode=send_frame:deint=interlaced"
        if p["height"]:
            vf += f",scale=-2:'min({p['height']},ih)':flags={p['scaler']}"
        self.pre_input = []
        if name == "videotoolbox":
            self.args = ["-c:v", "h264_videotoolbox", "-realtime", "1",
                         "-b:v", p["br"], "-maxrate", p["maxrate"], "-profile:v", "high"]
        elif name == "v4l2m2m":  # Raspberry Pi 4 (and other V4L2 hardware)
            vf += ",format=yuv420p"
            self.args = ["-c:v", "h264_v4l2m2m", "-b:v", p["br"]]
        elif name == "vaapi":    # Intel/AMD on Linux
            self.pre_input = ["-vaapi_device", VAAPI_DEVICE]
            vf += ",format=nv12,hwupload"
            self.args = ["-c:v", "h264_vaapi", "-b:v", p["br"], "-maxrate", p["maxrate"]]
        else:                    # software
            preset, crf, maxrate = p["x264"]
            self.args = ["-c:v", "libx264", "-preset", preset, "-tune", "zerolatency",
                         "-crf", crf, "-maxrate", maxrate,
                         "-bufsize", maxrate]
        self.vf = vf

    @property
    def hardware(self):
        return self.name != "x264"

    @property
    def two_stage(self):
        # The Raspberry Pi's V4L2 encoder emits SPS/PPS only once, so segments
        # after the first can't be decoded by a browser joining mid-stream.
        # Encoding to an MPEG-TS pipe and re-muxing with a second ffmpeg fixes
        # it: that ffmpeg learns SPS/PPS from the first frame and its muxer
        # repeats them before every keyframe. (Measured on a Pi 4: 1.52x
        # real time for 1080i, segments joinable; single-stage: not joinable.)
        return self.name == "v4l2m2m"

    def __str__(self):
        return f"{self.name}/{self.profile}"


def encoder_works(name):
    """Test-encode a few frames: an encoder can be listed but unusable."""
    enc = Encoder(name, "lite")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", *enc.pre_input,
           "-f", "lavfi", "-i", "testsrc2=s=640x360:d=0.3",
           "-vf", enc.vf.split(",", 1)[1], *enc.args,
           "-f", "null", "-"]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def pick_encoder(encoder="auto", profile="auto"):
    if encoder == "auto":
        listed = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                                capture_output=True, text=True).stdout
        candidates = [n for n, codec in (("videotoolbox", "h264_videotoolbox"),
                                         ("v4l2m2m", "h264_v4l2m2m"),
                                         ("vaapi", "h264_vaapi"))
                      if codec in listed]
        if "h264_vaapi" in listed and not os.path.exists(VAAPI_DEVICE):
            candidates.remove("vaapi")
        encoder = next((n for n in candidates if encoder_works(n)), "x264")
    if profile == "auto":
        if encoder != "x264":
            profile = "full"
        elif sys.platform.startswith("linux") and platform.machine() in ("aarch64", "armv7l"):
            profile = "lite"       # Raspberry Pi and similar boards
        else:
            profile = "balanced"
    return Encoder(encoder, profile)


class Stream:
    def __init__(self, channel, url, encoder):
        self.channel = channel
        # A unique directory per stream instance: when a stop and a restart of
        # the same channel overlap, the old stream's cleanup must not delete
        # the new stream's files (ffmpeg would die with ENOENT, exit 254).
        self.dir = Path(tempfile.mkdtemp(prefix=f"{channel}-", dir=WORK))
        self.last_access = self.started = time.time()
        self.stopped = False
        self.viewers = {}  # viewer id -> last playlist request (shared streams)
        self.encoder = encoder
        self.log = open(self.dir / "ffmpeg.log", "w")
        transcode = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            "-fflags", "+genpts+discardcorrupt",
            *encoder.pre_input,
            "-i", url,
            "-map", "0:v:0", "-map", "0:a:0?",
            "-vf", encoder.vf,
            *encoder.args,
            "-g", "60", "-force_key_frames", "expr:gte(t,n_forced*2)",
            # Fill gaps from dropped packets (e.g. Wi-Fi hiccups) with silence
            # so audio timestamps stay continuous and playback doesn't stutter.
            "-af", "aresample=async=1000:first_pts=0",
            "-c:a", "aac", "-b:a", "160k", "-ac", "2",
        ]
        hls_out = [
            "-f", "hls", "-hls_time", "2", "-hls_list_size", "10",
            "-hls_flags", "delete_segments+independent_segments+omit_endlist",
            "-hls_segment_filename", str(self.dir / "seg%05d.ts"),
            str(self.dir / "index.m3u8"),
        ]
        if encoder.two_stage:
            enc = subprocess.Popen(transcode + ["-f", "mpegts", "-"], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=self.log)
            mux = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "warning",
                                    "-f", "mpegts", "-i", "-", "-c", "copy", *hls_out],
                                   stdin=enc.stdout, stdout=subprocess.DEVNULL, stderr=self.log)
            enc.stdout.close()  # the muxer owns the read end now
            self.procs = [enc, mux]
        else:
            self.procs = [subprocess.Popen(transcode + hls_out, stdin=subprocess.DEVNULL,
                                           stdout=subprocess.DEVNULL, stderr=self.log)]

    @property
    def alive(self):
        return all(p.poll() is None for p in self.procs)

    @property
    def returncode(self):
        return next((p.returncode for p in self.procs if p.returncode is not None), None)

    def stalled(self):
        """True if ffmpeg is alive but hasn't written a segment in a while.

        Seen on a Raspberry Pi 4: the V4L2 encoder driver can wedge, leaving
        ffmpeg blocked in the kernel forever. It never exits, so without this
        the stream (and its tuner) would stay "busy" indefinitely.
        """
        segs = list(self.dir.glob("seg*.ts"))
        newest = max((f.stat().st_mtime for f in segs), default=self.started)
        limit = STALL_TIMEOUT if segs else STALL_TIMEOUT + 25  # allow time to tune
        return time.time() - newest > limit

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
              f"exit code {self.returncode}, ran {time.time() - self.started:.0f}s; "
              f"log: {self.error_text() or '(empty)'}", flush=True)

    def stop(self):
        self.stopped = True
        # Interrupt the first process; in two-stage mode the muxer then sees
        # end-of-input and finishes on its own.
        first = self.procs[0]
        if first.poll() is None:
            first.send_signal(signal.SIGINT)
        for p in self.procs:
            try:
                p.wait(3)
            except subprocess.TimeoutExpired:
                p.kill()
                try:
                    p.wait(2)
                except subprocess.TimeoutExpired:
                    # stuck in the kernel (e.g. a wedged hardware encoder);
                    # it will be reaped when it finally dies
                    print(f"ch {self.channel}: ffmpeg pid {p.pid} won't exit "
                          f"(stuck in the kernel?)", flush=True)
        self.log.close()
        shutil.rmtree(self.dir, ignore_errors=True)


class Manager:
    def __init__(self, device, encoder):
        self.device = device.rstrip("/")
        info = fetch_json(self.device + "/discover.json")
        self.info = info
        self.max_streams = int(info.get("TunerCount", 1))
        self.encoder = encoder
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

    def encoder_for_new_stream(self):
        """The Pi 4's V4L2 encoder wedged (process stuck in the kernel) when a
        second session started while one was running, so on V4L2 only one
        stream uses the hardware; others use software x264 at 720p."""
        if self.encoder.name == "v4l2m2m" and any(
                s.encoder.name == "v4l2m2m" for s in self.streams.values()):
            return Encoder("x264", "lite")
        return self.encoder

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
                s = Stream(channel, url, self.encoder_for_new_stream())
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

    def leave(self, channel, viewer):
        """A viewer left. Stop the stream only if nobody else is watching it."""
        with self.lock:
            s = self.streams.get(channel)
            if not s:
                return
            s.viewers.pop(viewer, None)
            now = time.time()
            if any(now - t < VIEWER_GRACE for t in s.viewers.values()):
                return  # someone else is still watching this channel
            self.streams.pop(channel)
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
            doomed = []
            with self.lock:
                for ch, s in list(self.streams.items()):
                    if not s.alive:
                        s.report("exited mid-stream")
                    elif s.stalled():
                        s.report(f"stalled (no new segment for {STALL_TIMEOUT}s+)")
                    elif now - s.last_access <= IDLE_TIMEOUT:
                        continue
                    doomed.append(self.streams.pop(ch))
            for s in doomed:  # outside the lock: stopping can take seconds
                s.stop()


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
            return self.send_json({"tuners": tuners, "encoder": self.mgr.encoder.args,
                                   "profile": str(self.mgr.encoder),
                                   "streams": {ch: {"profile": str(s.encoder), "args": s.encoder.args}
                                               for ch, s in self.mgr.streams.items()},
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
            viewer = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("c", ["anon"])[0]
            return self.serve_hls(m.group(1), m.group(2), viewer)
        return super().do_GET()

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        m = re.match(r"^/api/stop/([^/]+)$", u.path)
        if m:
            viewer = urllib.parse.parse_qs(u.query).get("c", ["anon"])[0]
            self.mgr.leave(m.group(1), viewer)
            return self.send_json({"ok": True})
        self.send_error(404)

    def serve_hls(self, channel, name, viewer="anon"):
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
        viewer = viewer[:64]
        s.viewers[viewer] = time.time()  # counts as watching while it tunes, too
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
                msg = s.error_text() or f"ffmpeg exited (code {s.returncode})"
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
        s.viewers[viewer] = time.time()
        self.send_file(s.dir / "index.m3u8", "application/vnd.apple.mpegurl")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1",
                    help="use 0.0.0.0 to allow other devices on your LAN")
    ap.add_argument("--device", help="HDHomeRun base URL, e.g. http://<hdhomerun-ip>")
    ap.add_argument("--profile", default="auto", choices=["auto", *PROFILES],
                    help="full = 1080p, balanced/lite = 720p with cheaper encoding "
                         "(auto: full with a hardware encoder, lite on ARM boards)")
    ap.add_argument("--encoder", default="auto",
                    choices=["auto", "videotoolbox", "v4l2m2m", "vaapi", "x264"],
                    help="H.264 encoder (auto: first working hardware encoder, else x264)")
    ap.add_argument("--workdir", default=None,
                    help="where HLS segments are written (default: system temp dir). "
                         "On a Raspberry Pi use a RAM-backed dir such as /run/hdhr or "
                         "/dev/shm to avoid wearing out the SD card")
    args = ap.parse_args()

    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found on PATH (macOS: brew install ffmpeg; "
                 "Debian/Raspberry Pi OS: sudo apt install ffmpeg)")
    global WORK
    WORK = Path(tempfile.mkdtemp(prefix="hdhr-web-", dir=args.workdir))
    device = args.device or discover()
    if not device:
        sys.exit("No HDHomeRun found; pass --device http://<ip>")

    encoder = pick_encoder(args.encoder, args.profile)
    if args.encoder != "auto" and args.encoder != "x264" and not encoder_works(args.encoder):
        sys.exit(f"encoder {args.encoder} doesn't work on this machine")
    Handler.mgr = mgr = Manager(device, encoder)
    print(f"{mgr.info.get('FriendlyName')} at {device} "
          f"({mgr.max_streams} tuners, encoder: {encoder})")
    try:
        srv = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as e:
        if e.errno in (48, 98):  # EADDRINUSE (macOS, Linux)
            sys.exit(f"Port {args.port} is already in use by another program. "
                     f"Pick another with --port (service: set HDHR_ARGS=\"--port N\" "
                     f"in /etc/default/hdhr).")
        raise
    srv.daemon_threads = True

    def _quit(*_):
        raise KeyboardInterrupt
    # Shut down cleanly (stopping ffmpeg, freeing tuners) on Ctrl-C or kill,
    # even when launched in the background where SIGINT is ignored.
    signal.signal(signal.SIGINT, _quit)
    signal.signal(signal.SIGTERM, _quit)
    if args.host in ("0.0.0.0", "::"):
        print(f"Listening on all interfaces, port {args.port} (open http://<this-machine>:{args.port})")
    else:
        print(f"Open http://{'localhost' if args.host == '127.0.0.1' else args.host}:{args.port}")
    print(f"Segments in {WORK}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        mgr.stop_all()
        shutil.rmtree(WORK, ignore_errors=True)


if __name__ == "__main__":
    main()
