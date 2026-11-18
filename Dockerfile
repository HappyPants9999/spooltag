FROM python:3.12.16-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data PUID=10001 PGID=10001
WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The code stays root-owned and read-only to the service user.
COPY app ./app
RUN groupadd --gid 10001 spooltag \
 && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin spooltag \
 && install -d -m 0700 -o 10001 -g 10001 /data
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"]
# No USER line on purpose: the entrypoint starts as root only to give an existing root-owned /data
# (spooltag <= 0.3) to PUID:PGID, then drops to that user before the server starts.
# Starting as non-root (compose `user: "10001:10001"`) also works if /data already belongs to it.
ENTRYPOINT ["python", "-m", "app.entrypoint"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
