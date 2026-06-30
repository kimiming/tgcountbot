FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# python:3.12-slim 已自带 ca-certificates，无需 apt-get（避免构建时访问 deb.debian.org）
COPY requirements.txt .
RUN pip install --no-cache-dir \
    -i https://pypi.tuna.tsinghua.edu.cn/simple \
    --trusted-host pypi.tuna.tsinghua.edu.cn \
    -r requirements.txt

COPY main_web.py .
COPY templates/ templates/

RUN mkdir -p sessions data

EXPOSE 8006

CMD ["python", "main_web.py"]
