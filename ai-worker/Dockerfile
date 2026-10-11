# CPU image (default):   docker build -t calibrex-ai ./ai-worker
# GPU image (CUDA 12.1): docker build -t calibrex-ai-gpu --build-arg TORCH_INDEX=https://download.pytorch.org/whl/cu121 ./ai-worker
FROM python:3.12-slim
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu
ARG INSTALL_H5_DEPS=0
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HF_HOME=/models/hf YOLO_CONFIG_DIR=/models/ultralytics
WORKDIR /srv
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir torch torchvision --index-url ${TORCH_INDEX}
COPY requirements.txt .
COPY requirements-h5.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN if [ "$INSTALL_H5_DEPS" = "1" ]; then pip install --no-cache-dir -r requirements-h5.txt; fi
COPY app ./app
COPY runs/detect/runs/detect/calibrex_s960/weights/best.pt /srv/calibrex-best.pt
RUN useradd -r -u 10001 calibrex && mkdir -p /models/hf /models/ultralytics/Ultralytics && chown -R calibrex:calibrex /models
USER calibrex
EXPOSE 8001
HEALTHCHECK --interval=20s --timeout=5s --start-period=120s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8001/health').status==200 else 1)"
# one process: the model lives on one GPU; scale by running more containers, not --workers
CMD ["uvicorn", "app.main:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8001"]
