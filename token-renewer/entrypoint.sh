#!/bin/sh
set -eu

: "${VNC_PASSWORD:?Set VNC_PASSWORD: noVNC shows a browser logged in to KMD}"

Xvfb :99 -screen 0 1280x900x24 -nolisten tcp &
sleep 1

# VNC listens on localhost only; noVNC on port 6080 is the way in.
x11vnc -storepasswd "$VNC_PASSWORD" /tmp/vncpass >/dev/null
x11vnc -display :99 -rfbauth /tmp/vncpass -localhost -forever -shared -quiet &
websockify --web /usr/share/novnc 6080 localhost:5900 >/dev/null 2>&1 &

exec python /app/renew.py
