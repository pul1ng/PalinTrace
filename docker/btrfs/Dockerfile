FROM ubuntu:24.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    python3 btrfs-progs util-linux ca-certificates && rm -rf /var/lib/apt/lists/*
CMD ["sleep", "infinity"]
