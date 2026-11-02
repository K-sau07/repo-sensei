# syntax=docker/dockerfile:1
# The registry proxy (8.2): dependency installs' only way out, to the package registries only
# (sensei_engine/sandbox/proxy.py, the standard library alone). Build from the repository root:
#   docker build -f sandbox/images/registry-proxy.Dockerfile -t repo-sensei/registry-proxy:1 \
#     packages/engine/src/sensei_engine/sandbox

FROM python:3.13-slim-trixie
# 0555: --chmod also sets the folder it creates, which must stay traversable.
COPY --chmod=0555 proxy.py /opt/proxy/proxy.py
USER 10001:10001
EXPOSE 3128
ENTRYPOINT ["python", "-I", "/opt/proxy/proxy.py", "--port", "3128"]
