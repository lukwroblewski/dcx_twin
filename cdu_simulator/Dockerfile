FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY simulator.py .
RUN useradd --create-home --shell /bin/bash appuser
USER appuser

EXPOSE 5020
CMD ["python", "-u", "simulator.py"]