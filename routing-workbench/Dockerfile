FROM rust:1.88.0-alpine3.21 AS builder

RUN apk add --no-cache build-base pkgconf

# Rustls still needs a native linker when building on Alpine.
ENV RUSTFLAGS="-C target-feature=-crt-static"

WORKDIR /app

# Cache dependencies by building with a dummy main.
COPY Cargo.toml ./
RUN mkdir src && echo "fn main() {}" > src/main.rs
RUN cargo build --release && rm -rf src

# Build the real application.
COPY static/ static/
COPY src/ src/
RUN touch src/main.rs && cargo build --release

FROM alpine:3.21

RUN apk add --no-cache ca-certificates tini libgcc

COPY --from=builder /app/target/release/routing-workbench /usr/local/bin/routing-workbench

EXPOSE 3000
ENTRYPOINT ["/sbin/tini", "--"]
CMD ["routing-workbench"]
