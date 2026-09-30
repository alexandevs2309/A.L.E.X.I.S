FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .
# CORE-03: el runtime oficial es `apps/demo` (CognitiveRuntime + PostgreSQL).
# `apps/api` es una fachada del mismo runtime y se sirve aparte, bajo demanda.
EXPOSE 8100
CMD ["python3", "-m", "apps.demo.server"]
