# Simulated-bench demo image: no hardware, no API key, no network needed at runtime.
#   docker build -t hands-sim .
#   docker run --rm hands-sim                       # all three gadgets
#   docker run --rm hands-sim demo steelers --sim --faults reversed_led,wrong_pin
#   docker run --rm -p 8765:8765 hands-sim guide steelers --web --host 0.0.0.0
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY hands ./hands
RUN pip install . && useradd --create-home --uid 10001 hands
USER hands
WORKDIR /home/hands

ENTRYPOINT ["hands"]
CMD ["demo", "all", "--sim"]
