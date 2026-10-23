#!/usr/bin/env bash
# 編一個「只會接檔、換包裝」的迷你 ffmpeg（不重新壓縮，所以不需要任何編碼器）
# 用法：build.sh <FFmpeg 原始碼資料夾> <輸出資料夾> [windows]
#   帶 windows 參數 → 在 Linux 上用 mingw-w64 編 Windows 版（GitHub Actions 用）
#   不帶 → 編本機版（本機測試用）
# 授權：沒有用 --enable-gpl / --enable-nonfree，所以是 LGPL 版本
set -euo pipefail
SRC="$1"; OUT="$2"; TARGET="${3:-native}"
mkdir -p "$OUT"
cd "$SRC"

COMMON=(
  --disable-everything --disable-autodetect --disable-network --disable-doc --disable-debug
  --disable-ffplay --enable-ffmpeg --enable-ffprobe
  --disable-avdevice --disable-swscale --disable-postproc --disable-x86asm
  --enable-small --enable-static --disable-shared
  # 讀寫檔案
  --enable-protocol=file,pipe
  # 讀得懂：MP4/MOV、TS（切片）、MKV/WebM、FLV、純 AAC/MP3、H.264/H.265 原始串流
  --enable-demuxer=mov,mpegts,matroska,flv,aac,mp3,h264,hevc,concat
  # 寫得出：MP4、MOV、MKV、WebM、TS、純 AAC
  --enable-muxer=mp4,mov,ipod,matroska,webm,mpegts,adts,null
  # 不解碼也能讀出畫面大小、聲音格式（換包裝時要用）
  --enable-parser=h264,hevc,aac,aac_latm,mpegaudio,av1,vp9,opus,vorbis
  # 換包裝時需要的轉換：TS 的 AAC 放進 MP4、時間戳修正（yt-dlp 會檢查 setts）
  --enable-bsf=aac_adtstoasc,h264_mp4toannexb,hevc_mp4toannexb,extract_extradata,setts,vp9_superframe,null
  # 聲音解碼器：不會拿來重新壓縮，只是讀取取樣率和聲道數；少了它，TS 裡的 AAC 放不進 MP4
  --enable-decoder=aac,aac_latm,mp3float,opus
  # ffmpeg 命令列本身需要的最少濾鏡
  --enable-filter=null,anull,aresample,format,aformat,copy,acopy
)

if [ "$TARGET" = "windows" ]; then
  ./configure "${COMMON[@]}" --target-os=mingw32 --arch=x86_64 --cross-prefix=x86_64-w64-mingw32- \
    --extra-ldflags="-static -static-libgcc" --pkg-config=false
  EXE=.exe; STRIP=x86_64-w64-mingw32-strip
else
  ./configure "${COMMON[@]}" --extra-ldflags="-static"
  EXE=; STRIP=strip
fi
make -j"$(nproc)" "ffmpeg$EXE" "ffprobe$EXE"
$STRIP "ffmpeg$EXE" "ffprobe$EXE"
cp "ffmpeg$EXE" "ffprobe$EXE" "$OUT/"
ls -la "$OUT"
