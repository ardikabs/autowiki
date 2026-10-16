FROM cgr.dev/chainguard/wolfi-base:latest

RUN apk add --no-cache \
      python-3.12 \
      py3.12-pip \
      nginx \
      git \
      ca-certificates \
    && python3.12 -m venv /opt/venv

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY lib/runner.py /app/runner.py
COPY conf/nginx.conf /etc/nginx/nginx.conf

RUN mkdir -p /srv/docs /repos /run /tmp/nginx/client_temp

EXPOSE 8080

CMD ["/opt/venv/bin/python", "/app/runner.py"]