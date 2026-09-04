FROM python:3.13-slim

# opencv needs a couple of system libs at runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir \
    numpy-stl==2.16.3 "numpy>=2.1.3" \
    opencv-python-headless \
    pydantic pyyaml scipy "stl>=0.0.3" \
    fastapi "uvicorn[standard]" python-multipart

COPY . .

ENV DATA_DIR=/data
VOLUME ["/data"]

EXPOSE 8080
CMD ["python", "-m", "uvicorn", "src.api:app", "--host", "0.0.0.0", "--port", "8080"]
