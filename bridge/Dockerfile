FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY yemot_bridge.py store.py saic_client.py .
COPY vehicles/ ./vehicles/
ENV PORT=8000
EXPOSE 8000
CMD ["python", "yemot_bridge.py"]
