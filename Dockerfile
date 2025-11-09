# Base image. Used CUDA 11 for compatibility with sherpa onnx GPU version
FROM nvidia/cuda:11.8.0-cudnn8-runtime-ubuntu20.04 AS base

# Set noninteractive mode for apt
ENV DEBIAN_FRONTEND=noninteractive

# Update and install dependencies
RUN apt-get update && apt-get install -y git curl && apt-get install -y --no-install-recommends ffmpeg

# Create non-root user with UID 1000 and GID 1000
RUN groupadd -g 1000 vtuber && \
    useradd -m -u 1000 -g 1000 -s /bin/bash vtuber

# uv is really good. they even have a distro-less binary
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Create app directory and set ownership
RUN mkdir -p /home/vtuber/app && chown -R vtuber:vtuber /home/vtuber/app

# Set working directory
WORKDIR /home/vtuber/app

# Copy from the cache instead of linking since it's a mounted volume
ENV UV_LINK_MODE=copy

USER vtuber

COPY --chown=vtuber:vtuber . /home/vtuber/app

# Switch to non-root user

# Install the project's dependencies using the lockfile and settings
# Run as root but use vtuber's cache directory with proper permissions
RUN --mount=type=cache,target=/home/vtuber/.cache/uv,uid=1000,gid=1000 \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev --find-links https://k2-fsa.github.io/sherpa/onnx/cuda.html --extra-index-url https://aiinfra.pkgs.visualstudio.com/PublicPackages/_packaging/onnxruntime-cuda-11/pypi/simple/
# Copy the rest of the project source code with proper ownership

# Install oh-my-bash for better shell experience
RUN bash -c "$(curl -fsSL https://raw.githubusercontent.com/ohmybash/oh-my-bash/master/tools/install.sh)"

# Expose port 12393 (the new default port)
EXPOSE 12393

# Run the application
CMD ["uv", "run", "run_server.py"]
