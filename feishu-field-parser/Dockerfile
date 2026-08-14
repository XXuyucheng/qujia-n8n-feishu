# Docker Hub 在国内常超时，默认走镜像站；可覆盖：
# docker compose build --build-arg BASE_IMAGE=python:3.12-slim feishu-field-parser
ARG BASE_IMAGE=docker.1ms.run/library/python:3.12-slim
FROM ${BASE_IMAGE}

WORKDIR /app

COPY requirements.txt .
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com/debian-security|g' \
      /etc/apt/sources.list.d/debian.sources 2>/dev/null || true \
 && sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com/debian-security|g' \
      /etc/apt/sources.list 2>/dev/null || true

COPY app.py .

EXPOSE 8020

CMD ["python", "app.py"]
