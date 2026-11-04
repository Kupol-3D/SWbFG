# Source-pinned FGMS; apt packages and base-image tag are not bit-for-bit pinned.
# Runtime/build require a separate Docker smoke test.
FROM ubuntu:24.04 AS builder
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    build-essential cmake git ca-certificates libcrypt-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git init fgms && cd fgms \
    && git remote add origin https://github.com/FlightGear/fgms.git \
    && git fetch --depth 1 origin 26e3dbd4124a10ea825806b77d32e046527cfe9d \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = 26e3dbd4124a10ea825806b77d32e046527cfe9d
RUN cmake -S fgms -B build -DCMAKE_BUILD_TYPE=Release \
    && cmake --build build --parallel 2

FROM ubuntu:24.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    libstdc++6 libcrypt1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /src/build/fgms /usr/local/bin/fgms
COPY --from=builder /src/fgms/COPYING /usr/share/doc/fgms/COPYING
USER 65534:65534
WORKDIR /tmp
STOPSIGNAL SIGTERM
# Port declaration is metadata only; compose publishes on an explicit host IP.
EXPOSE 5000/udp
ENTRYPOINT ["/usr/local/bin/fgms"]
CMD ["-d", "-c", "/config/fgms.conf"]
