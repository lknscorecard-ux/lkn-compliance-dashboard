# ── LKN Compliance Pipeline — Docker Image ─────────────────────────────────
# Base: Python 3.11 slim (small, fast)
FROM python:3.11-slim

WORKDIR /app

# System deps (needed for python-calamine C extension)
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies
COPY requirements_cloud.txt .
RUN pip install --no-cache-dir -r requirements_cloud.txt

# Engine modules
COPY engine_recipe.py \
     engine_ingredient.py \
     engine_bidfood.py \
     engine_opalion.py \
     engine_compliance.py \
     ./

# Static reference files (baked into the image — update when files change)
COPY PLU_Mapping_Complete.xlsx .
COPY ["Recipe builder.xlsx", "."]

# Main job
COPY run_pipeline_cloud.py .

# Run the pipeline
CMD ["python", "run_pipeline_cloud.py"]
