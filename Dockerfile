FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .
WORKDIR /work
ENTRYPOINT ["giraffe"]
