# Sandbox image the benchmark agent runs inside. The point of containment:
# the agent (claude CLI + its shell tools) gets ONLY its mounted workdir and
# a network path to the host proctor / Elasticsearch / model API -- it cannot
# read the host filesystem, so it cannot find a plaintext answer key. The
# proctor (which holds the answers) runs on the HOST and is reached over HTTP.
#
# One image serves both capability profiles (elastic-only just doesn't use the
# forensic tools). Built for the host's arch by `docker build`.
FROM node:22-bookworm

# Forensic + general shell tooling the agent may need. Grounded in what
# scenario 2's later steps require (see harness/orchestrator/capability_profiles.py):
# Sleuthkit (disk-image/MFT), UPX (unpacking), hashcat (key/hash cracking),
# binutils/xxd (PE/ELF inspection), p7zip/unzip (artifact extraction).
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip python3-dev \
      build-essential libtsk-dev libffi-dev \
      sleuthkit hashcat \
      binutils xxd file p7zip-full unzip curl jq bsdmainutils ca-certificates xz-utils \
    && rm -rf /var/lib/apt/lists/*

# UPX isn't in bookworm's main apt (it's in contrib); grab the static release
# binary for the build arch instead.
RUN UPXV=4.2.4; ARCH=$(dpkg --print-architecture); \
    curl -fsSL -o /tmp/upx.tar.xz "https://github.com/upx/upx/releases/download/v${UPXV}/upx-${UPXV}-${ARCH}_linux.tar.xz" && \
    tar -xJf /tmp/upx.tar.xz -C /tmp && \
    install -m 0755 "/tmp/upx-${UPXV}-${ARCH}_linux/upx" /usr/local/bin/upx && \
    rm -rf /tmp/upx* && upx --version | head -1

# Python forensic libs (profile: pefile / python-evtx / regipy / pytsk3).
# --break-system-packages: fine inside a throwaway container (PEP 668).
RUN pip3 install --no-cache-dir --break-system-packages \
      pefile python-evtx regipy pytsk3 pyyaml requests

# The agent itself. Pinned to match the host CLI version.
RUN npm install -g @anthropic-ai/claude-code@2.1.201

# Run as the base image's non-root `node` user (uid 1000), no sudo --
# defense in depth inside the sandbox. /work is the bind-mounted run workdir.
RUN mkdir -p /work && chown node:node /work
USER node
WORKDIR /work
