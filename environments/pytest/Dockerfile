# Build explicitly; the verifier never pulls or installs packages automatically.
ARG BASE_IMAGE=python:3.12.15-slim
FROM ${BASE_IMAGE}
USER root
RUN python -m pip install --no-cache-dir pytest==8.4.2
WORKDIR /workspace
USER 65534:65534
ENTRYPOINT ["python"]
