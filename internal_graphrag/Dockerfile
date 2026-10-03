FROM python:3.12-slim
WORKDIR /app
COPY internal_graphrag/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY internal_graphrag /app/internal_graphrag
CMD ["python", "-m", "internal_graphrag.cli", "ingest", "--feed", "I"]
