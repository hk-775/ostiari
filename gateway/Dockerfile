FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
COPY ostiari_sidecar/ ostiari_sidecar/

RUN pip install --no-cache-dir .

EXPOSE 8421

ENTRYPOINT ["ostiari-sidecar"]
CMD ["--host", "0.0.0.0", "--port", "8421"]
