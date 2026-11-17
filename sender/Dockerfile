# 发送器镜像（可选，profile=tools）：本地发送器也可以在宿主机直接用 python 运行
FROM python:3.12-slim

WORKDIR /sender
ENV SENDER_BASE=http://backend:8000
COPY . .
ENTRYPOINT ["python", "sender.py"]
