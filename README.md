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
