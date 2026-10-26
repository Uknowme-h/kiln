# Image for one generated-app test run.
# The runner adds --network none, CPU and memory caps, and mounts only the workspace.
FROM python:3.12-slim

RUN pip install --no-cache-dir pytest

WORKDIR /workspace

# Generated projects are installed into this image at run time only when they
# vendor a requirements.txt. The base image stays offline during the test run.
CMD ["pytest", "-q"]
