# Docker Hub 在国内常超时，默认走镜像站；可覆盖：
# docker compose build --build-arg BASE_IMAGE=python:3.12-slim supplier-bot
ARG BASE_IMAGE=docker.1ms.run/library/python:3.12-slim
FROM ${BASE_IMAGE}

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    -i https://pypi.tuna.tsinghua.edu.cn/simple

COPY app.py feishu_client.py session_store.py extractor.py validator.py dialog.py config_loader.py router.py ./
COPY config ./config
COPY test_*.py ./

EXPOSE 8040

CMD ["python", "app.py"]
