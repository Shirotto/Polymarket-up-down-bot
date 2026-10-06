FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY updown_bot ./updown_bot
RUN pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1 DATA_DIR=/app/data
VOLUME /app/data
CMD ["python", "-m", "updown_bot", "--no-tui"]
