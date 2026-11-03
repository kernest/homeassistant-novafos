# Chromium and Playwright for Python, plus a virtual display served through
# noVNC for the occasional human login.
FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

RUN apt-get update \
    && apt-get install -y --no-install-recommends xvfb x11vnc novnc websockify \
    && rm -rf /var/lib/apt/lists/*

# The image ships the browsers, not the Python package; match its version.
RUN python -m pip install --no-cache-dir --break-system-packages playwright==1.49.0

WORKDIR /app
COPY renew.py entrypoint.sh ./
RUN chmod +x entrypoint.sh

ENV DATA_DIR=/data DISPLAY=:99
VOLUME /data
EXPOSE 6080

ENTRYPOINT ["/app/entrypoint.sh"]
