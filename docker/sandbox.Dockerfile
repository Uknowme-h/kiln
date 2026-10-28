# Image for one generated-app test run.
# The runner adds --network none, CPU and memory caps, and mounts only the workspace.
FROM python:3.12-slim

# The narrow stack the requirements agent chooses from. The sandbox stays
# offline during test runs, so these must be baked in: pytest for the suite,
# Flask because every golden-prompt app is a small Flask service. SQLite is
# stdlib. When the stack grows, this image grows with it — never the network.
RUN pip install --no-cache-dir pytest flask

WORKDIR /workspace

# Generated projects are installed into this image at run time only when they
# vendor a requirements.txt. The base image stays offline during the test run.
CMD ["pytest", "-q"]
