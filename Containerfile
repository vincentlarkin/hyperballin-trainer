FROM docker.io/library/ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv libpython3.10 python3-tk python3-gi gir1.2-glib-2.0 \
    binutils patchelf ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY requirements-build.txt ./
RUN python3 -m venv --system-site-packages /opt/build-env && \
    /opt/build-env/bin/python -m pip install --no-cache-dir -r requirements-build.txt
COPY trainer.py hyperballin-trainer.spec ./
RUN /opt/build-env/bin/python -m PyInstaller --clean --noconfirm hyperballin-trainer.spec
COPY LICENSE ./
RUN mkdir -p dist/licenses && cp LICENSE dist/LICENSE && \
    for notice in /usr/share/doc/*/copyright; do \
      package="$(basename "$(dirname "$notice")")"; \
      cp -L "$notice" "dist/licenses/$package.txt"; \
    done && \
    cp /opt/build-env/lib/python3.10/site-packages/pyinstaller-6.22.0.dist-info/licenses/COPYING.txt \
       dist/licenses/PyInstaller.txt
