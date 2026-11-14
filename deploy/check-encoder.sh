#!/usr/bin/env bash
# Check which H.264 encoder setups produce HLS segments a browser can join
# mid-stream (each segment must carry SPS/PPS headers), and how fast each is.
#   ./deploy/check-encoder.sh [channel]      (default channel 9.1, a 1080i one)
# Needs one free tuner for ~12 s. Prints a table; paste it back if asked.
set -u
CH=${1:-9.1}
DIR=$(mktemp -d)
trap 'rm -rf "$DIR"' EXIT

BASE=$(python3 -c 'import sys; sys.path.insert(0, "/opt/hdhr"); sys.path.insert(0, "."); import server; print(server.discover_local() or "")' 2>/dev/null)
HOST=${BASE#http://}; HOST=${HOST%%:*}; HOST=${HOST%%/*}
[ -n "$HOST" ] || { echo "Couldn't find the HDHomeRun on the LAN" >&2; exit 1; }
echo "ffmpeg: $(ffmpeg -version | head -1)"
echo "device: $HOST   channel: $CH"
echo "recording 12 s sample..."
curl -s --max-time 12 "http://$HOST:5004/auto/v$CH" -o "$DIR/sample.ts"
[ -s "$DIR/sample.ts" ] || { echo "No data (tuner busy?)" >&2; exit 1; }
SRC_DUR=10  # seconds of the sample each variant encodes
now() { python3 -c 'import time; print(time.time())'; }

DEINT="bwdif=mode=send_frame:deint=interlaced"
try() {  # name, video filter, encoder args...
  local name=$1 vf=$2; shift 2
  local out="$DIR/$name"; mkdir -p "$out"
  local t0=$(now)
  ffmpeg -hide_banner -loglevel error -i "$DIR/sample.ts" -t "$SRC_DUR" -map 0:v:0 -map 0:a:0? \
    -vf "$vf" "$@" -g 60 -force_key_frames 'expr:gte(t,n_forced*2)' \
    -c:a aac -b:a 160k -ac 2 -f hls -hls_time 2 -hls_list_size 0 \
    -hls_segment_filename "$out/seg%03d.ts" "$out/index.m3u8" 2> "$out/err.txt"
  report "$name" $? "$t0" "$out"
}

report() {  # name, rc, t0, outdir
  local name=$1 rc=$2 t0=$3 out=$4
  local t1=$(now)
  local speed=$(awk -v d="$SRC_DUR" -v a="$t0" -v b="$t1" 'BEGIN { printf "%.2f", d / (b - a) }')
  local seg="$out/seg002.ts" res="-" joinable="no"
  if [ $rc -ne 0 ] || [ ! -f "$seg" ]; then
    printf "%-26s FAILED: %s\n" "$name" "$(grep -v 'frame dimensions\|repeated' "$out/err.txt" | head -c 140 | tr '\n' ' ')"; return
  fi
  # a later segment decoded on its own = what a browser joining mid-stream sees
  if ! ffmpeg -v error -i "$seg" -f null - 2>&1 | grep -q "non-existing PPS\|no frame"; then joinable="yes"; fi
  res=$(ffprobe -v error -select_streams v -show_entries stream=width,height -of csv=p=0:s=x "$seg" | head -1)
  local kbps=$(( $(wc -c < "$seg") * 8 / 2000 ))
  printf "%-26s joinable=%-3s  %-9s  speed=%sx  %s kbps\n" "$name" "$joinable" "${res:-?}" "$speed" "$kbps"
}

# Two-stage: encode to a plain MPEG-TS pipe, then copy into HLS. The second
# ffmpeg learns SPS/PPS from the first frame, so its muxer can repeat them
# before every keyframe even if the encoder only emits them once.
try2() {  # name, video filter, encoder args...
  local name=$1 vf=$2; shift 2
  local out="$DIR/$name"; mkdir -p "$out"
  local t0=$(now)
  ffmpeg -hide_banner -loglevel error -i "$DIR/sample.ts" -t "$SRC_DUR" -map 0:v:0 -map 0:a:0? \
    -vf "$vf" "$@" -g 60 -force_key_frames 'expr:gte(t,n_forced*2)' \
    -c:a aac -b:a 160k -ac 2 -f mpegts - 2> "$out/err.txt" |
  ffmpeg -hide_banner -loglevel error -f mpegts -i - -c copy -f hls -hls_time 2 -hls_list_size 0 \
    -hls_segment_filename "$out/seg%03d.ts" "$out/index.m3u8" 2>> "$out/err.txt"
  report "$name" $? "$t0" "$out"
}

echo; echo "speed > 1.0x = keeps up with live TV"
try v4l2m2m           "$DEINT,format=yuv420p" -c:v h264_v4l2m2m -b:v 10M
try v4l2m2m+dump     "$DEINT,format=yuv420p" -c:v h264_v4l2m2m -b:v 10M -bsf:v dump_extra=freq=keyframe
try v4l2m2m+ghdr+dump "$DEINT,format=yuv420p" -c:v h264_v4l2m2m -b:v 10M -flags:v +global_header -bsf:v dump_extra=freq=keyframe
try v4l2m2m-720+dump "$DEINT,scale=-2:720,format=yuv420p" -c:v h264_v4l2m2m -b:v 5M -bsf:v dump_extra=freq=keyframe
[ "${SELFTEST:-}" ] && try2 x264-2stage "$DEINT,scale=-2:720" -c:v libx264 -preset ultrafast
try2 v4l2m2m-2stage    "$DEINT,format=yuv420p" -c:v h264_v4l2m2m -b:v 10M
try2 v4l2m2m-720-2stage "$DEINT,scale=-2:720,format=yuv420p" -c:v h264_v4l2m2m -b:v 5M
try x264-lite-720    "$DEINT,scale=-2:720:flags=fast_bilinear" -c:v libx264 -preset ultrafast -tune zerolatency -crf 23 -maxrate 5M -bufsize 5M
