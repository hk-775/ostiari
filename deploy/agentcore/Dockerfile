FROM python:3.12-alpine@sha256:4b5b91c1040aeb90137a7ed5235e3caf7a1a45da015066c5305fb64e70262ba2

WORKDIR /app

COPY deploy/agentcore/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY deploy/agentcore/app.py ./

RUN addgroup -S -g 10001 ostiari \
    && adduser -S -D -H -u 10001 -G ostiari ostiari

USER 10001:10001
EXPOSE 8080

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
