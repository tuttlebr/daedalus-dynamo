FROM rust:1.88.0-alpine3.21 AS builder

RUN apk add --no-cache build-base pkgconf libheif-dev

# libheif on Alpine is shared-only, so disable musl static linking
ENV RUSTFLAGS="-C target-feature=-crt-static"

WORKDIR /app

# Cache dependencies by building with a dummy main
COPY Cargo.toml ./
RUN mkdir src && echo "fn main() {}" > src/main.rs
RUN cargo build --release && rm -rf src

# Build the real application
COPY static/ static/
COPY src/ src/
RUN touch src/main.rs && cargo build --release

# Runtime image
FROM alpine:3.21

RUN apk add --no-cache ca-certificates tini libheif

COPY --from=builder /app/target/release/photo-tagger /usr/local/bin/photo-tagger

EXPOSE 3000
ENTRYPOINT ["/sbin/tini", "--"]
CMD ["photo-tagger"]
