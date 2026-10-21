# One container: the host, Pandoc, and XeLaTeX. All state lives in /data.
FROM python:3.13-slim

ARG PANDOC_VERSION=3.6.4
ARG TARGETARCH=amd64

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl \
      texlive-xetex texlive-latex-recommended texlive-latex-extra texlive-fonts-recommended \
      lmodern fonts-lmodern \
 && curl -fsSL -o /tmp/pandoc.deb \
      "https://github.com/jgm/pandoc/releases/download/${PANDOC_VERSION}/pandoc-${PANDOC_VERSION}-1-${TARGETARCH}.deb" \
 && apt-get install -y --no-install-recommends /tmp/pandoc.deb \
 && rm /tmp/pandoc.deb \
 && apt-get purge -y curl && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY spec ./spec
RUN uv sync --frozen --no-dev

# Run as an unprivileged user. Its home holds TeX and font caches, so /data holds only state.
RUN useradd --system --uid 10001 --create-home --home-dir /home/bevis bevis \
 && mkdir -p /data && chown bevis:bevis /data

ENV BEVIS_DATA_DIR=/data \
    PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1
USER bevis
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"
CMD ["bevis", "serve", "--host", "0.0.0.0", "--port", "8000"]
