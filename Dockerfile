FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .
RUN useradd --create-home --uid 10001 giraffe && mkdir -p /work && chown giraffe:giraffe /work
USER giraffe
WORKDIR /work
ENTRYPOINT ["giraffe"]
