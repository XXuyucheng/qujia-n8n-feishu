# Docker Hub 在国内常超时，默认走镜像站。
ARG BASE_IMAGE=docker.1ms.run/library/python:3.12-slim
FROM ${BASE_IMAGE}

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    -i https://pypi.tuna.tsinghua.edu.cn/simple

COPY app.py feishu_client.py session_store.py validator.py rules.py \
     config_loader.py cards.py card_engine.py handlers.py ws_client.py ./
COPY config ./config
COPY test_*.py ./

EXPOSE 8050

CMD ["python", "app.py"]
