# syntax=docker/dockerfile:1
# Source images and engine versions: config/model-catalog/nvidia-spark.json.
ARG SPARK_PLATFORM=linux/arm64
FROM --platform=${SPARK_PLATFORM} nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.6.1@sha256:5899e7bdd3528ab09a17ec87e2abe1949f8a0386a0a49f3b24dbd52d8211dc5d AS dynamo-native
COPY runtime/nvidia-spark/export_native_runtime.py /tmp/export_native_runtime.py
RUN python3 /tmp/export_native_runtime.py /opt/native-export

FROM --platform=${SPARK_PLATFORM} eugr/spark-vllm:nightly-20261014@sha256:7094664bb85abf28be8cc5d678d8fc2a954160479d31f3a8402d394f74eb5dca
USER root
COPY --from=dynamo-native /opt/native-export /opt/dynamo-native
ENV LD_LIBRARY_PATH=/opt/dynamo-native/lib:${LD_LIBRARY_PATH}
COPY runtime/nvidia-spark/*.py /opt/nvidia-spark/
COPY config/model-catalog/nvidia-spark.json /opt/nvidia-spark/nvidia-spark.json
# Do not install ai-dynamo[vllm]: it would replace the selected engine.
RUN python3 /opt/nvidia-spark/install_dynamo.py \
 && python3 /opt/nvidia-spark/verify_runtime.py --runtime spark-20261014

LABEL io.daedalus.nvidia-recipes.commit="7b2ee453234617f58e1e100c195ac3be7f79e233" \
      io.daedalus.dynamo.version="1.6.1"
WORKDIR /workspace
ENTRYPOINT ["/bin/bash"]
