#!/usr/bin/env bash
set -e
if command -v apt-get &> /dev/null; then
    apt-get update -qq
    apt-get install -y --no-install-recommends aria2 ffmpeg
fi
pip install --upgrade pip
pip install -r requirements.txt
mkdir -p "${DOWNLOADS_DIR:-/tmp/downloads}"
