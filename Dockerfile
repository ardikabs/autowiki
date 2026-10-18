FROM cgr.dev/chainguard/wolfi-base:latest

ARG VERSION=dev

LABEL org.opencontainers.image.title="autowiki" \
      org.opencontainers.image.description="Git-backed documentation portal" \
      org.opencontainers.image.source="https://github.com/ardikabs/autowiki" \
      org.opencontainers.image.version="${VERSION}"

RUN apk add --no-cache \
      python-3.12 \
      py3.12-pip \
      nginx \
      git \
      ca-certificates \
    && (getent group nginx || addgroup -S nginx) \
    && (getent passwd nginx || adduser -S -D -h /var/cache/nginx -s /sbin/nologin -G nginx nginx) \
    && python3.12 -m venv /opt/venv

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    AUTOWIKI_VERSION="${VERSION}"

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY lib/runner.py /app/runner.py
COPY site/index.html /site/index.html
COPY conf/nginx.conf /etc/nginx/nginx.conf

RUN mkdir -p /srv/docs /repos /run /tmp/nginx/client_temp /var/lib/nginx/tmp /var/log/nginx \
    && chown -R nginx:nginx /var/lib/nginx /var/log/nginx /tmp/nginx

EXPOSE 8080

CMD ["/opt/venv/bin/python", "/app/runner.py"]
