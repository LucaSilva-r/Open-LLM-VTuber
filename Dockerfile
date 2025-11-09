# Base image. Used CUDA 11 for compatibility with sherpa onnx GPU version
FROM nvidia/cuda:11.6.1-cudnn8-runtime-ubuntu20.04 AS base

# uv is really good. they even have a distro-less binary
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/


# Set noninteractive mode for apt
ENV DEBIAN_FRONTEND=noninteractive

# Update and install dependencies
RUN apt-get update && apt-get install -y git curl && apt-get install -y --no-install-recommends ffmpeg

# Set working directory
WORKDIR /app

# Copy from the cache instead of linking since it's a mounted volume
ENV UV_LINK_MODE=copy

# Install the project's dependencies using the lockfile and settings
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev --find-links https://k2-fsa.github.io/sherpa/onnx/cuda.html --extra-index-url https://aiinfra.pkgs.visualstudio.com/PublicPackages/_packaging/onnxruntime-cuda-11/pypi/simple/
#    uv pip uninstall onnxruntime sherpa-onnx faster-whisper

# Then, add the rest of the project source code and install it
# Installing separately from its dependencies allows optimal layer caching
COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --inexact --find-links https://k2-fsa.github.io/sherpa/onnx/cuda.html --extra-index-url https://aiinfra.pkgs.visualstudio.com/PublicPackages/_packaging/onnxruntime-cuda-11/pypi/simple/

# Expose port 12393 (the new default port)
EXPOSE 12393

RUN bash -c "$(curl -fsSL https://raw.githubusercontent.com/ohmybash/oh-my-bash/master/tools/install.sh)"


CMD ["uv", "run", "run_server.py"]