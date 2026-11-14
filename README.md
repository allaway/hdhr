# hdhr

Watch live TV from an HDHomeRun in any browser. One Python file (standard library only) plus one HTML page.

HDHomeRun tuners send MPEG-2/AC3 broadcast streams, which browsers can't play, so the server transcodes the selected channel to HLS (H.264/AAC) with ffmpeg. On a Mac it uses the hardware encoder (VideoToolbox); elsewhere it falls back to libx264.

## Run

```sh
brew install ffmpeg        # or: apt install ffmpeg
python3 server.py          # finds the HDHomeRun automatically
open http://localhost:8080
```

Options:

- `--device http://<hdhomerun-ip>` skips auto-discovery
- `--host 0.0.0.0` lets other devices on your LAN (phones, TVs, VPN clients) use it
- `--port 8080`
- `--profile auto|full|balanced|lite`: transcoding quality vs CPU (see below)
- `--encoder auto|videotoolbox|v4l2m2m|vaapi|x264`

## Profiles and low-power devices

Nearly all the CPU goes to deinterlacing and H.264 encoding. Decoding MPEG-2, converting audio, and the Python server itself are cheap. So the profiles trade output resolution and encoder effort:

| Profile | Video | Hardware encoder | Software (x264) |
|---|---|---|---|
| `full` | 1080p30 / 720p60 | 10 Mbps | veryfast |
| `balanced` | ≤720p | 6 Mbps | superfast |
| `lite` | ≤720p, fast scaler | 4 Mbps | ultrafast |

All profiles deinterlace with `bwdif` (about 3× cheaper than `yadif` at similar quality), and only for frames that are interlaced, so 720p60 channels pass through at full frame rate.

`auto` picks the first hardware encoder that passes a test encode (VideoToolbox on macOS, V4L2 on a Raspberry Pi 4, VA-API on Intel/AMD Linux). It then uses `full` with that encoder, `lite` with x264 on ARM boards, and `balanced` otherwise.

Measured on an Apple M-series Mac (ffmpeg CPU, % of one core, per stream):

| Setup | 1080i channel | 720p channel |
|---|---|---|
| VideoToolbox `full` (1080p) | ~19% | — |
| x264 `lite` (720p) | ~29% | ~28% |
| x264 at 1080p (old default) | ~156% | — |

A Raspberry Pi core is several times slower than these, so on a Pi 5 (no hardware encoder) use `lite`. On a Pi 4, the hardware encoder (`v4l2m2m`) handles the encode. The V4L2 and VA-API paths haven't been tested on real hardware yet.

## Features

- **Channel list** with station logos, network, the current show and its progress.
- **TV guide** (`G`): a scrolling time grid. Click a show to tune its channel. Guide data comes from SiliconDust's guide API, the same one their apps use, authorized by the tuner's own DeviceAuth token.
- **Info panel** (`I`): live stream stats with text charts covering signal, source and output bitrate, buffer, latency, and dropped frames.
- **Favorites** (★), search across channels and current shows (`/`), fullscreen (`F`), mute (`M`).
- Remembers the last channel, favorites, and volume. If the browser blocks autoplay with sound, the first click or keypress unmutes.

## Behavior notes

- **Latency:** playback runs about 5–10 s behind the broadcast; a channel change takes about 6–7 s (HLS with 2 s segments).
- **Tuners:** each channel being watched uses one tuner. A tuner is released when you change channels or close the page (or after 30 s without requests). If every tuner is busy and the app holds one, it gives up its least-recently-watched stream; it never takes a tuner from another device.
- **Recovery:** if playback stalls for 12 s or the connection drops, the page retunes automatically.
- **Reception glitches:** dropped audio packets are filled with silence so audio stays in sync. To tell reception errors from network errors, run `hdhomerun_config <device-id> get /tuner0/debug`: a rising `ts: te=` count means reception errors, and a rising `net: err=` count means network errors.
- Temporary HLS files live in a temp directory, which is deleted on exit.
