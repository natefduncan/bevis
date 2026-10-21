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

ENV BEVIS_DATA_DIR=/data \
    PATH=/app/.venv/bin:$PATH
VOLUME /data
EXPOSE 8000
CMD ["bevis", "serve", "--host", "0.0.0.0", "--port", "8000"]
