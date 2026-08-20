# Docker Hub 在国内常超时，默认走镜像站；可覆盖：
# docker compose build --build-arg BASE_IMAGE=python:3.12-slim bill-parser
ARG BASE_IMAGE=docker.1ms.run/library/python:3.12-slim
FROM ${BASE_IMAGE}

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    -i https://pypi.tuna.tsinghua.edu.cn/simple

COPY app.py parser.py config.yaml ./

EXPOSE 8050

CMD ["python", "app.py"]
