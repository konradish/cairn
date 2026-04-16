FROM python:3.12-slim

# git: build.py shells out to `git log` on mounted project paths.
# ca-certificates: stdlib HTTPS-friendly defaults (not required today, cheap).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir PyYAML==6.0.2

# Run as non-root. The app only reads mounts and writes index.html inside /app.
RUN useradd --create-home --shell /bin/bash --uid 1000 cairn
WORKDIR /app
COPY --chown=cairn:cairn build.py server.py /app/
COPY --chown=cairn:cairn templates/ /app/templates/

# build.py reads git history from mounted project dirs. Git refuses to
# operate on repos owned by a different UID unless they're marked safe.
# The mounts come in as root-owned from the Windows host, so allow all.
RUN git config --system --add safe.directory '*'

USER cairn
EXPOSE 8080
CMD ["python", "server.py"]
