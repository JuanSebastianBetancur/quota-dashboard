FROM python:3.12-slim

WORKDIR /app

COPY config.example.json ./config.example.json
COPY server.py ./server.py
RUN cp config.example.json config.json

EXPOSE 8765

CMD ["python3", "-u", "server.py"]
