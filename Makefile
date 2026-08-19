SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

CHART ?= charts/dynamo-model
VALUES_DIR ?= values
MODELS := $(sort $(basename $(notdir $(wildcard $(VALUES_DIR)/*.yaml))))

MODEL ?= qwen35b
MODEL_NAME := $(if $(filter nemotron omni,$(MODEL)),nemotron-omni,$(MODEL))
MODEL_VALUES ?= $(VALUES_DIR)/$(MODEL_NAME).yaml
RELEASE ?= $(MODEL_NAME)

NAMESPACE ?= daedalus-dynamo
APP_LABEL_VALUE ?= daedalus-dynamo-demo
HF_TOKEN_SECRET ?= hf-token-secret
GATEWAY_NAME ?= inference-gateway
GATEWAY ?= 1
INSTALL_GATEWAY ?= 0
GATEWAY_REQUEST_TIMEOUT ?= 300s
GATEWAY_PORT ?= 32359
GATEWAY_NODE_PORT ?= 32359
AGENTGATEWAY_VERSION ?= v1.3.1
AGENTGATEWAY_IMAGE ?= cr.agentgateway.dev/agentgateway:$(AGENTGATEWAY_VERSION)
GATEWAY_DOCKERFILE ?= Dockerfile.agentgateway
GATEWAY_IMAGE ?= daedalus.ddns.me:5050/btuttle/daedalus-agentgateway:$(AGENTGATEWAY_VERSION)
GATEWAY_IMAGE_REGISTRY := $(shell printf '%s\n' '$(GATEWAY_IMAGE)' | sed 's|/.*||')
GATEWAY_IMAGE_REPOSITORY := $(shell printf '%s\n' '$(GATEWAY_IMAGE)' | sed 's|^[^/]*/||; s/:[^/:]*$$//')
GATEWAY_IMAGE_TAG := $(shell printf '%s\n' '$(GATEWAY_IMAGE)' | sed 's/^.*://')
GATEWAY_IMAGE_PULL_POLICY ?= IfNotPresent
GATEWAY_REPLICAS ?= 1
GATEWAY_NOFILE_LIMIT ?= 131072
GATEWAY_CPU_REQUEST ?= 500m
GATEWAY_CPU_LIMIT ?= 4
GATEWAY_MEMORY_REQUEST ?= 512Mi
GATEWAY_MEMORY_LIMIT ?= 2Gi
GATEWAY_HPA_MIN_REPLICAS ?= 2
GATEWAY_HPA_MAX_REPLICAS ?= 8
GATEWAY_HPA_CPU_UTILIZATION ?= 70
GATEWAY_POOL_IDLE_TIMEOUT ?= 30s
GATEWAY_POOL_MAX_SIZE ?= 64
GATEWAY_HTTP1_IDLE_TIMEOUT ?= 60s
GATEWAY_MAX_CONNECTION_DURATION ?= 10m
GATEWAY_SHUTDOWN_MIN_SECONDS ?= 15
GATEWAY_SHUTDOWN_MAX_SECONDS ?= 330
GATEWAY_FD_EXPORTER_IMAGE ?= python:3.12-alpine
GATEWAY_FD_METRICS_PORT ?= 9090
PROMETHEUS_RELEASE ?= kube-prometheus-stack
MODEL_CATALOG_IMAGE ?= python:3.12-alpine
EXPECTED_CONTEXT ?= kubernetes-admin@kubernetes
GROVE_VERSION ?= v0.1.0-alpha.12-rc1
DYNAMO_PLATFORM_VERSION ?= 1.4.0
GROVE_TERMINATION_DELAY ?= 15m
SCHEDULING_RECOVERY_TIMEOUT ?= 45m

HELM ?= helm
KUBECTL ?= kubectl
DOCKER ?= docker
PYTHON ?= python3

DOCKERFILE ?= Dockerfile
CONTEXT ?= .
BASE_IMAGE ?= nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.3.1
DOCKER_BUILD_ARGS ?=

MODEL_IMAGE_FROM_VALUES := $(shell sed -n '/^model:[[:space:]]*$$/,/^[^[:space:]][^:]*:/s/^[[:space:]]*image:[[:space:]]*//p' "$(MODEL_VALUES)" 2>/dev/null | head -1 | tr -d '"')
MODEL_DGD_FROM_VALUES := $(shell sed -n '/^model:[[:space:]]*$$/,/^[^[:space:]][^:]*:/s/^[[:space:]]*name:[[:space:]]*//p' "$(MODEL_VALUES)" 2>/dev/null | head -1 | tr -d '"')
MODEL_ID_FROM_VALUES := $(shell sed -n '/^model:[[:space:]]*$$/,/^[^[:space:]][^:]*:/s/^[[:space:]]*id:[[:space:]]*//p' "$(MODEL_VALUES)" 2>/dev/null | head -1 | tr -d '"')
MODEL_IMAGE_REPOSITORY := $(shell printf '%s\n' '$(MODEL_IMAGE_FROM_VALUES)' | sed 's/:[^/:]*$$//')
MODEL_IMAGE_TAG := $(shell printf '%s\n' '$(MODEL_IMAGE_FROM_VALUES)' | sed 's/^.*://')
MODEL_DGD ?= $(MODEL_DGD_FROM_VALUES)
MODEL_ID ?= $(MODEL_ID_FROM_VALUES)
MODEL_ROUTE_NAME ?= daedalus-dynamo-$(MODEL_NAME)

REGISTRY ?= daedalus.ddns.me:5050/btuttle
IMAGE_WAS_SET := $(if $(filter undefined,$(origin IMAGE)),,1)
IMAGE_REPOSITORY_WAS_SET := $(if $(filter undefined,$(origin IMAGE_REPOSITORY)),,1)
IMAGE_TAG_WAS_SET := $(if $(filter undefined,$(origin IMAGE_TAG)),,1)
IMAGE_REPOSITORY ?= $(if $(MODEL_IMAGE_REPOSITORY),$(MODEL_IMAGE_REPOSITORY),$(REGISTRY)/daedalus-dynamo-vllm-runtime)
IMAGE_TAG ?= $(if $(MODEL_IMAGE_TAG),$(MODEL_IMAGE_TAG),latest)
IMAGE ?= $(IMAGE_REPOSITORY):$(IMAGE_TAG)

DEPLOY_ALL_IMAGE_GOALS := deploy-all-check deploy-all-images
ifneq ($(filter $(DEPLOY_ALL_IMAGE_GOALS),$(MAKECMDGOALS)),)
DEPLOY_ALL_IMAGES := $(sort $(shell \
	for model in $(MODELS); do \
		image="$$(sed -n '/^model:[[:space:]]*$$/,/^[^[:space:]][^:]*:/s/^[[:space:]]*image:[[:space:]]*//p' "$(VALUES_DIR)/$$model.yaml" 2>/dev/null | head -1 | tr -d '"')"; \
		repository="$$(printf '%s\n' "$$image" | sed 's/:[^/:]*$$//')"; \
		tag="$$(printf '%s\n' "$$image" | sed 's/^.*://')"; \
		if [ -n "$(IMAGE_WAS_SET)" ]; then \
			resolved_image='$(IMAGE)'; \
		else \
			if [ -n "$(IMAGE_REPOSITORY_WAS_SET)" ]; then repository='$(IMAGE_REPOSITORY)'; fi; \
			if [ -n "$(IMAGE_TAG_WAS_SET)" ]; then tag='$(IMAGE_TAG)'; fi; \
			if [ -z "$$repository" ]; then repository='$(REGISTRY)/daedalus-dynamo-vllm-runtime'; fi; \
			if [ -z "$$tag" ]; then tag=latest; fi; \
			resolved_image="$$repository:$$tag"; \
		fi; \
		printf '%s\n' "$$resolved_image"; \
	done))
endif
DEPLOY_ALL_RELEASE_TARGETS := $(addprefix deploy-all-release-,$(MODELS))
DEPLOY_ALL_ROUTE_TARGETS := $(addprefix deploy-all-route-,$(MODELS))

DEPLOYMENT_MODE ?=
HELM_ARGS ?=
HELM_LINT_ARGS ?=
HELM_TEMPLATE_ARGS ?=
HELM_UPGRADE_ARGS ?=
HELM_PACKAGE_ARGS ?=
HELM_COMPAT_HOOK_ENABLED ?= false
TAKE_OWNERSHIP ?= 0
CREATE_NAMESPACE ?= 1
SKIP_BUILD ?= 0
SKIP_PUSH ?= 0
SKIP_SECRET ?= 0
SKIP_GATEWAY_BUILD ?= 0
SKIP_GATEWAY_PUSH ?= 0
PLANNER_COMPAT ?= 0
DIST_DIR ?= dist

TAKE_OWNERSHIP_ARG := $(if $(filter 1 true yes,$(TAKE_OWNERSHIP)),--take-ownership,)
CREATE_NAMESPACE_ARG := $(if $(filter 1 true yes,$(CREATE_NAMESPACE)),--create-namespace,)
HELM_MODE_ARG := $(if $(strip $(DEPLOYMENT_MODE)),--set-string model.deploymentMode=$(DEPLOYMENT_MODE),)
HELM_MODEL_ARGS := --set-string model.image=$(IMAGE) --set-string model.envFromSecret=$(HF_TOKEN_SECRET) --set planner.compatHook.enabled=$(HELM_COMPAT_HOOK_ENABLED) $(HELM_MODE_ARG)

.DEFAULT_GOAL := help

.PHONY: help
help:
	@printf '%s\n' \
		'Usage:' \
		'  make <target> MODEL=<model>' \
		'  make <target>-<model>' \
		'' \
		'Models:' \
		'  $(MODELS)' \
		'' \
		'Common targets:' \
		'  make build MODEL=qwen35b          Build the model runtime image.' \
		'  make push MODEL=qwen35b           Push the model runtime image.' \
		'  make deploy MODEL=qwen35b         Build, push, ensure HF token secret, then Helm upgrade/install.' \
		'  make upgrade MODEL=qwen35b        Helm upgrade/install only.' \
		'  make gateway MODEL=qwen35b        Apply inference-gateway resources and model route.' \
		'  make gateway-image                 Build and push the hardened agentgateway image.' \
		'  make scheduling-resilience         Upgrade Grove recovery and repair stuck pod gangs.' \
		'  make check-scheduling-resilience   Read-only recovery/version check.' \
		'  make render MODEL=qwen35b         Render the Helm manifests.' \
		'  make lint MODEL=qwen35b           Lint the chart with the model values.' \
		'  make uninstall MODEL=qwen35b      Helm uninstall the model release.' \
		'  make status MODEL=qwen35b         Show Helm release status.' \
		'' \
		'All-model targets:' \
		'  make lint-all' \
		'  make deploy-all' \
		'  make -j5 deploy-all             Parallelize independent model releases and routes.' \
		'  make uninstall-all' \
		'' \
		'Useful variables:' \
		'  NAMESPACE=$(NAMESPACE)' \
		'  RELEASE=$(RELEASE)' \
		'  IMAGE=$(IMAGE)' \
		'  DEPLOYMENT_MODE=direct|dgdr       Optional Helm override for model.deploymentMode.' \
		'  TAKE_OWNERSHIP=1                  Pass --take-ownership to Helm 3.17+.' \
		'  SKIP_BUILD=1 SKIP_PUSH=1          Skip image steps during deploy.' \
		'  SKIP_SECRET=1                     Skip HF token secret check/update during deploy.' \
		'  GATEWAY=0                         Skip inference-gateway resources during deploy.' \
		'  INSTALL_GATEWAY=1                 Install/upgrade Gateway API, GAIE, and agentgateway first.' \
		'  GATEWAY_REPLICAS=1                Minimum gateway deployment replica count.' \
		'  GATEWAY_NOFILE_LIMIT=131072       Gateway process RLIMIT_NOFILE soft limit.' \
		'  EXPECTED_CONTEXT=$(EXPECTED_CONTEXT)  Required live-cluster context guard.' \
		'  GROVE_TERMINATION_DELAY=15m       Maximum healthy-to-stuck gang recovery delay.' \
		'  DYNAMO_PLATFORM_VERSION=1.4.0     Minimum platform paired with the Grove release.' \
		'  SKIP_GATEWAY_BUILD=1              Reuse an already built hardened gateway image.' \
		'  SKIP_GATEWAY_PUSH=1               Reuse an already pushed hardened gateway image.' \
		'  HF_TOKEN=<token>                  Create or update $(HF_TOKEN_SECRET).' \
		'' \
		'Model aliases:' \
		'  MODEL=nemotron and MODEL=omni map to MODEL=nemotron-omni.'

.PHONY: models
models:
	@printf '%s\n' $(MODELS)

.PHONY: check-model
check-model:
	@test -f "$(MODEL_VALUES)" || { \
		echo "ERROR: unknown MODEL=$(MODEL) (resolved to $(MODEL_NAME))."; \
		echo "Available models: $(MODELS)"; \
		exit 2; \
	}
	@test -n "$(MODEL_DGD)" || { echo "ERROR: could not read model.name from $(MODEL_VALUES)."; exit 2; }
	@test -n "$(MODEL_ID)" || { echo "ERROR: could not read model.id from $(MODEL_VALUES)."; exit 2; }

.PHONY: check-helm check-kubectl check-docker check-python
check-helm:
	@command -v "$(HELM)" >/dev/null || { echo "ERROR: $(HELM) not found."; exit 127; }

check-kubectl:
	@command -v "$(KUBECTL)" >/dev/null || { echo "ERROR: $(KUBECTL) not found."; exit 127; }

check-docker:
	@command -v "$(DOCKER)" >/dev/null || { echo "ERROR: $(DOCKER) not found."; exit 127; }

check-python:
	@command -v "$(PYTHON)" >/dev/null || { echo "ERROR: $(PYTHON) not found."; exit 127; }

.PHONY: build
build: check-model check-docker
	@echo "==> Building $(IMAGE) for $(MODEL_NAME)"
	@build_args=(); \
	if [[ -n "$(BASE_IMAGE)" ]]; then \
		build_args+=(--build-arg "BASE_IMAGE=$(BASE_IMAGE)"); \
	fi; \
	$(DOCKER) build "$${build_args[@]}" $(DOCKER_BUILD_ARGS) -t "$(IMAGE)" -f "$(DOCKERFILE)" "$(CONTEXT)"

.PHONY: push
push: check-model check-docker
	@echo "==> Pushing $(IMAGE)"
	@$(DOCKER) push "$(IMAGE)"

.PHONY: image
image: build push

.PHONY: build-gateway-image push-gateway-image gateway-image
build-gateway-image: check-docker
	@echo "==> Building hardened gateway image $(GATEWAY_IMAGE)"
	@$(DOCKER) build \
		--build-arg "AGENTGATEWAY_IMAGE=$(AGENTGATEWAY_IMAGE)" \
		-t "$(GATEWAY_IMAGE)" \
		-f "$(GATEWAY_DOCKERFILE)" \
		"$(CONTEXT)"

push-gateway-image: check-docker
	@echo "==> Pushing hardened gateway image $(GATEWAY_IMAGE)"
	@$(DOCKER) push "$(GATEWAY_IMAGE)"

gateway-image: build-gateway-image push-gateway-image

.PHONY: namespace
namespace: check-kubectl
	@if [[ "$(CREATE_NAMESPACE)" =~ ^(1|true|yes)$$ ]]; then \
		$(KUBECTL) create namespace "$(NAMESPACE)" --dry-run=client -o yaml | $(KUBECTL) apply -f -; \
	else \
		$(KUBECTL) get namespace "$(NAMESPACE)" >/dev/null; \
	fi

.PHONY: secret
secret: namespace
	@if [[ -n "$${HF_TOKEN:-}" ]]; then \
		echo "==> Creating/updating $(HF_TOKEN_SECRET) in namespace $(NAMESPACE)"; \
		$(KUBECTL) create secret generic "$(HF_TOKEN_SECRET)" \
			--from-literal=HF_TOKEN="$${HF_TOKEN}" \
			-n "$(NAMESPACE)" \
			--dry-run=client -o yaml | $(KUBECTL) apply -f -; \
		$(KUBECTL) label secret "$(HF_TOKEN_SECRET)" \
			-n "$(NAMESPACE)" \
			app.kubernetes.io/part-of="$(APP_LABEL_VALUE)" \
			--overwrite >/dev/null; \
	elif $(KUBECTL) get secret "$(HF_TOKEN_SECRET)" -n "$(NAMESPACE)" >/dev/null 2>&1; then \
		echo "==> Reusing existing $(HF_TOKEN_SECRET) in namespace $(NAMESPACE)"; \
	else \
		echo "ERROR: $(HF_TOKEN_SECRET) does not exist in namespace $(NAMESPACE). Set HF_TOKEN or create the secret first."; \
		exit 1; \
	fi

.PHONY: lint
lint: check-model check-helm
	@$(HELM) lint "$(CHART)" \
		-f "$(MODEL_VALUES)" \
		$(HELM_MODEL_ARGS) \
		$(HELM_ARGS) \
		$(HELM_LINT_ARGS)

.PHONY: render template
render template: check-model check-helm
	@$(HELM) template "$(RELEASE)" "$(CHART)" \
		--namespace "$(NAMESPACE)" \
		-f "$(MODEL_VALUES)" \
		$(HELM_MODEL_ARGS) \
		$(HELM_ARGS) \
		$(HELM_TEMPLATE_ARGS)

.PHONY: dry-run
dry-run: check-model check-helm
	@$(HELM) upgrade --install "$(RELEASE)" "$(CHART)" \
		--namespace "$(NAMESPACE)" \
		$(CREATE_NAMESPACE_ARG) \
		-f "$(MODEL_VALUES)" \
		$(HELM_MODEL_ARGS) \
		$(TAKE_OWNERSHIP_ARG) \
		--dry-run=client \
		--debug \
		$(HELM_ARGS) \
		$(HELM_UPGRADE_ARGS)

.PHONY: upgrade install
upgrade install: check-model check-helm
	@echo "==> Helm upgrade/install $(RELEASE) from $(MODEL_VALUES) into namespace $(NAMESPACE)"
	@$(HELM) upgrade --install "$(RELEASE)" "$(CHART)" \
		--namespace "$(NAMESPACE)" \
		$(CREATE_NAMESPACE_ARG) \
		-f "$(MODEL_VALUES)" \
		$(HELM_MODEL_ARGS) \
		$(TAKE_OWNERSHIP_ARG) \
		$(HELM_ARGS) \
		$(HELM_UPGRADE_ARGS)

.PHONY: deploy
deploy: check-model
	@if [[ ! "$(SKIP_BUILD)" =~ ^(1|true|yes)$$ ]]; then \
		$(MAKE) --no-print-directory build MODEL="$(MODEL)"; \
	fi
	@if [[ ! "$(SKIP_PUSH)" =~ ^(1|true|yes)$$ ]]; then \
		$(MAKE) --no-print-directory push MODEL="$(MODEL)"; \
	fi
	@if [[ ! "$(SKIP_SECRET)" =~ ^(1|true|yes)$$ ]]; then \
		$(MAKE) --no-print-directory secret MODEL="$(MODEL)"; \
	fi
	@if [[ "$(INSTALL_GATEWAY)" =~ ^(1|true|yes)$$ ]]; then \
		$(MAKE) --no-print-directory install-gateway MODEL="$(MODEL)"; \
	elif [[ "$(GATEWAY)" =~ ^(1|true|yes)$$ ]]; then \
		$(MAKE) --no-print-directory gateway-base gateway-monitoring MODEL="$(MODEL)"; \
	fi
	@$(MAKE) --no-print-directory upgrade MODEL="$(MODEL)"
	@if [[ "$(PLANNER_COMPAT)" =~ ^(1|true|yes)$$ ]]; then \
		$(MAKE) --no-print-directory planner-compat MODEL="$(MODEL)"; \
	fi
	@if [[ "$(GATEWAY)" =~ ^(1|true|yes)$$ ]]; then \
		$(MAKE) --no-print-directory gateway-route-apply MODEL="$(MODEL)"; \
		$(MAKE) --no-print-directory gateway-catalog-apply MODEL="$(MODEL)"; \
	fi

.PHONY: uninstall delete
uninstall delete: check-model check-helm
	@$(HELM) uninstall "$(RELEASE)" --namespace "$(NAMESPACE)" --ignore-not-found
	@if [[ "$(GATEWAY)" =~ ^(1|true|yes)$$ ]]; then \
		$(MAKE) --no-print-directory gateway-unroute MODEL="$(MODEL)"; \
		$(MAKE) --no-print-directory gateway-catalog MODEL="$(MODEL)"; \
	fi

.PHONY: status
status: check-model check-helm
	@$(HELM) status "$(RELEASE)" --namespace "$(NAMESPACE)"

.PHONY: history
history: check-model check-helm
	@$(HELM) history "$(RELEASE)" --namespace "$(NAMESPACE)"

.PHONY: package
package: check-helm
	@mkdir -p "$(DIST_DIR)"
	@$(HELM) package "$(CHART)" --destination "$(DIST_DIR)" $(HELM_PACKAGE_ARGS)

.PHONY: install-gateway
install-gateway: check-kubectl check-helm check-python
	@if [[ ! "$(SKIP_GATEWAY_BUILD)" =~ ^(1|true|yes)$$ ]]; then \
		$(MAKE) --no-print-directory build-gateway-image; \
	fi
	@if [[ ! "$(SKIP_GATEWAY_PUSH)" =~ ^(1|true|yes)$$ ]]; then \
		$(MAKE) --no-print-directory push-gateway-image; \
	fi
	@AGENTGATEWAY_VERSION="$(AGENTGATEWAY_VERSION)" scripts/install-inference-gateway.sh
	@$(MAKE) --no-print-directory gateway-base gateway-monitoring MODEL="$(MODEL)"

.PHONY: gateway gateway-base gateway-route gateway-route-apply gateway-unroute gateway-catalog gateway-catalog-apply gateway-monitoring
gateway: gateway-base gateway-monitoring
	@$(MAKE) --no-print-directory gateway-route-apply MODEL="$(MODEL)"
	@$(MAKE) --no-print-directory gateway-catalog-apply MODEL="$(MODEL)"

gateway-base: namespace
	@for crd in \
		agentgatewayparameters.agentgateway.dev \
		agentgatewaypolicies.agentgateway.dev \
		gateways.gateway.networking.k8s.io \
		httproutes.gateway.networking.k8s.io; do \
		if ! $(KUBECTL) get crd "$$crd" >/dev/null 2>&1; then \
			echo "ERROR: missing CRD $$crd. Run make install-gateway, then retry."; \
			exit 1; \
		fi; \
	done
	@echo "==> Applying $(GATEWAY_NAME) base resources in namespace $(NAMESPACE)"
	@APP_LABEL_VALUE="$(APP_LABEL_VALUE)" \
		NAMESPACE="$(NAMESPACE)" \
		GATEWAY_NAME="$(GATEWAY_NAME)" \
		GATEWAY_REQUEST_TIMEOUT="$(GATEWAY_REQUEST_TIMEOUT)" \
		GATEWAY_PORT="$(GATEWAY_PORT)" \
		GATEWAY_NODE_PORT="$(GATEWAY_NODE_PORT)" \
		GATEWAY_IMAGE_REGISTRY="$(GATEWAY_IMAGE_REGISTRY)" \
		GATEWAY_IMAGE_REPOSITORY="$(GATEWAY_IMAGE_REPOSITORY)" \
		GATEWAY_IMAGE_TAG="$(GATEWAY_IMAGE_TAG)" \
		GATEWAY_IMAGE_PULL_POLICY="$(GATEWAY_IMAGE_PULL_POLICY)" \
		GATEWAY_REPLICAS="$(GATEWAY_REPLICAS)" \
		GATEWAY_NOFILE_LIMIT="$(GATEWAY_NOFILE_LIMIT)" \
		GATEWAY_CPU_REQUEST="$(GATEWAY_CPU_REQUEST)" \
		GATEWAY_CPU_LIMIT="$(GATEWAY_CPU_LIMIT)" \
		GATEWAY_MEMORY_REQUEST="$(GATEWAY_MEMORY_REQUEST)" \
		GATEWAY_MEMORY_LIMIT="$(GATEWAY_MEMORY_LIMIT)" \
		GATEWAY_HPA_MIN_REPLICAS="$(GATEWAY_HPA_MIN_REPLICAS)" \
		GATEWAY_HPA_MAX_REPLICAS="$(GATEWAY_HPA_MAX_REPLICAS)" \
		GATEWAY_HPA_CPU_UTILIZATION="$(GATEWAY_HPA_CPU_UTILIZATION)" \
		GATEWAY_POOL_IDLE_TIMEOUT="$(GATEWAY_POOL_IDLE_TIMEOUT)" \
		GATEWAY_POOL_MAX_SIZE="$(GATEWAY_POOL_MAX_SIZE)" \
		GATEWAY_HTTP1_IDLE_TIMEOUT="$(GATEWAY_HTTP1_IDLE_TIMEOUT)" \
		GATEWAY_MAX_CONNECTION_DURATION="$(GATEWAY_MAX_CONNECTION_DURATION)" \
		GATEWAY_SHUTDOWN_MIN_SECONDS="$(GATEWAY_SHUTDOWN_MIN_SECONDS)" \
		GATEWAY_SHUTDOWN_MAX_SECONDS="$(GATEWAY_SHUTDOWN_MAX_SECONDS)" \
		GATEWAY_FD_EXPORTER_IMAGE="$(GATEWAY_FD_EXPORTER_IMAGE)" \
		GATEWAY_FD_METRICS_PORT="$(GATEWAY_FD_METRICS_PORT)" \
		scripts/render-inference-gateway.sh base | $(KUBECTL) apply -n "$(NAMESPACE)" -f -

gateway-monitoring: namespace
	@if $(KUBECTL) get crd servicemonitors.monitoring.coreos.com >/dev/null 2>&1 && \
		$(KUBECTL) get crd prometheusrules.monitoring.coreos.com >/dev/null 2>&1; then \
		echo "==> Applying $(GATEWAY_NAME) FD monitoring and alerts"; \
		APP_LABEL_VALUE="$(APP_LABEL_VALUE)" \
			NAMESPACE="$(NAMESPACE)" \
			GATEWAY_NAME="$(GATEWAY_NAME)" \
			GATEWAY_HPA_MIN_REPLICAS="$(GATEWAY_HPA_MIN_REPLICAS)" \
			PROMETHEUS_RELEASE="$(PROMETHEUS_RELEASE)" \
			scripts/render-inference-gateway.sh monitoring | $(KUBECTL) apply -n "$(NAMESPACE)" -f -; \
	else \
		echo "WARN: Prometheus Operator CRDs are absent; FD metrics Service remains available but ServiceMonitor/alerts were skipped."; \
	fi

gateway-route: gateway-base
	@$(MAKE) --no-print-directory gateway-route-apply MODEL="$(MODEL)"

gateway-route-apply: check-model check-kubectl
	@echo "==> Applying $(GATEWAY_NAME) route for $(MODEL_ID)"
	@APP_LABEL_VALUE="$(APP_LABEL_VALUE)" \
		GATEWAY_NAME="$(GATEWAY_NAME)" \
		GATEWAY_REQUEST_TIMEOUT="$(GATEWAY_REQUEST_TIMEOUT)" \
		MODEL_ROUTE_NAME="$(MODEL_ROUTE_NAME)" \
		MODEL_ID="$(MODEL_ID)" \
		MODEL_DGD="$(MODEL_DGD)" \
		scripts/render-inference-gateway.sh route | $(KUBECTL) apply -n "$(NAMESPACE)" -f -

gateway-unroute: check-model check-kubectl
	@$(KUBECTL) delete httproute "$(MODEL_ROUTE_NAME)" -n "$(NAMESPACE)" --ignore-not-found

gateway-catalog: namespace
	@$(MAKE) --no-print-directory gateway-catalog-apply MODEL="$(MODEL)"

gateway-catalog-apply: check-kubectl check-python
	@echo "==> Refreshing model catalog from deployed model routes"
	@models_json="$$( \
		$(KUBECTL) get httproute \
			-n "$(NAMESPACE)" \
			-l "app.kubernetes.io/part-of=$(APP_LABEL_VALUE),daedalus-dynamo/route-kind=model" \
			-o json 2>/dev/null | \
			$(PYTHON) scripts/http-routes-to-models-json.py \
	)"; \
	APP_LABEL_VALUE="$(APP_LABEL_VALUE)" \
		MODEL_CATALOG_IMAGE="$(MODEL_CATALOG_IMAGE)" \
		MODELS_JSON="$${models_json}" \
		scripts/render-inference-gateway.sh catalog | $(KUBECTL) apply -n "$(NAMESPACE)" -f -

.PHONY: planner-compat
planner-compat: check-model check-kubectl
	@echo "==> Planner namespace compatibility is handled by Helm via planner.workerNamespaceCompat=legacy."
	@echo "==> No live DGD annotation patch is applied; operator-owned worker hash annotations are left untouched."

.PHONY: doctor validate-resilience
doctor: check-kubectl
	@NAMESPACE="$(NAMESPACE)" scripts/cluster-doctor.sh

validate-resilience: check-helm check-python
	@scripts/validate-gateway-resilience.sh

.PHONY: scheduling-resilience check-scheduling-resilience
scheduling-resilience: check-kubectl check-helm check-python
	@EXPECTED_CONTEXT="$(EXPECTED_CONTEXT)" \
		NAMESPACE="$(NAMESPACE)" \
		GROVE_VERSION="$(GROVE_VERSION)" \
		DYNAMO_PLATFORM_VERSION="$(DYNAMO_PLATFORM_VERSION)" \
		GROVE_TERMINATION_DELAY="$(GROVE_TERMINATION_DELAY)" \
		SCHEDULING_RECOVERY_TIMEOUT="$(SCHEDULING_RECOVERY_TIMEOUT)" \
		scripts/install-scheduling-resilience.sh

check-scheduling-resilience: check-kubectl check-helm check-python
	@EXPECTED_CONTEXT="$(EXPECTED_CONTEXT)" \
		NAMESPACE="$(NAMESPACE)" \
		GROVE_VERSION="$(GROVE_VERSION)" \
		DYNAMO_PLATFORM_VERSION="$(DYNAMO_PLATFORM_VERSION)" \
		GROVE_TERMINATION_DELAY="$(GROVE_TERMINATION_DELAY)" \
		scripts/install-scheduling-resilience.sh --check

.PHONY: lint-all deploy-all deploy-all-check deploy-all-images deploy-all-shared deploy-all-releases deploy-all-routes uninstall-all
.PHONY: $(DEPLOY_ALL_RELEASE_TARGETS) $(DEPLOY_ALL_ROUTE_TARGETS)
lint-all:
	@for model in $(MODELS); do \
		echo "==> lint $$model"; \
		$(MAKE) --no-print-directory lint MODEL="$$model"; \
	done

deploy-all:
	@$(MAKE) --no-print-directory deploy-all-check
	@$(MAKE) --no-print-directory deploy-all-images
	@$(MAKE) --no-print-directory deploy-all-shared
	@$(MAKE) --no-print-directory deploy-all-releases
	@if [[ "$(GATEWAY)" =~ ^(1|true|yes)$$ ]]; then \
		$(MAKE) --no-print-directory deploy-all-routes; \
		$(MAKE) --no-print-directory gateway-catalog-apply; \
	fi

deploy-all-check:
	@for model in $(MODELS); do \
		$(MAKE) --no-print-directory check-model MODEL="$$model"; \
	done
	@test -n "$(strip $(DEPLOY_ALL_IMAGES))" || { echo "ERROR: no model images resolved."; exit 2; }
	@$(MAKE) --no-print-directory check-helm
	@if [[ ! "$(SKIP_BUILD)" =~ ^(1|true|yes)$$ ]] || [[ ! "$(SKIP_PUSH)" =~ ^(1|true|yes)$$ ]]; then \
		$(MAKE) --no-print-directory check-docker; \
	fi
	@if [[ ! "$(SKIP_SECRET)" =~ ^(1|true|yes)$$ ]] || \
		[[ "$(GATEWAY)" =~ ^(1|true|yes)$$ ]] || \
		[[ "$(INSTALL_GATEWAY)" =~ ^(1|true|yes)$$ ]]; then \
		$(MAKE) --no-print-directory check-kubectl; \
	fi
	@if [[ "$(GATEWAY)" =~ ^(1|true|yes)$$ ]] || [[ "$(INSTALL_GATEWAY)" =~ ^(1|true|yes)$$ ]]; then \
		$(MAKE) --no-print-directory check-python; \
	fi

deploy-all-images:
	@if [[ ! "$(SKIP_BUILD)" =~ ^(1|true|yes)$$ ]]; then \
		echo "==> Building shared runtime image with tags: $(DEPLOY_ALL_IMAGES)"; \
		build_args=(); \
		if [[ -n "$(BASE_IMAGE)" ]]; then \
			build_args+=(--build-arg "BASE_IMAGE=$(BASE_IMAGE)"); \
		fi; \
		image_args=(); \
		for image in $(DEPLOY_ALL_IMAGES); do \
			image_args+=(-t "$$image"); \
		done; \
		$(DOCKER) build "$${build_args[@]}" "$${image_args[@]}" $(DOCKER_BUILD_ARGS) -f "$(DOCKERFILE)" "$(CONTEXT)"; \
	fi
	@if [[ ! "$(SKIP_PUSH)" =~ ^(1|true|yes)$$ ]]; then \
		for image in $(DEPLOY_ALL_IMAGES); do \
			echo "==> Pushing $$image"; \
			$(DOCKER) push "$$image"; \
		done; \
	fi

deploy-all-shared:
	@if [[ "$(INSTALL_GATEWAY)" =~ ^(1|true|yes)$$ ]]; then \
		if [[ ! "$(SKIP_SECRET)" =~ ^(1|true|yes)$$ ]]; then \
			$(MAKE) --no-print-directory secret MODEL="$(firstword $(MODELS))"; \
		fi; \
		$(MAKE) --no-print-directory install-gateway MODEL="$(firstword $(MODELS))"; \
	elif [[ "$(GATEWAY)" =~ ^(1|true|yes)$$ ]]; then \
		shared_targets=(gateway-base gateway-monitoring); \
		if [[ ! "$(SKIP_SECRET)" =~ ^(1|true|yes)$$ ]]; then \
			shared_targets=(secret "$${shared_targets[@]}"); \
		fi; \
		$(MAKE) --no-print-directory "$${shared_targets[@]}" MODEL="$(firstword $(MODELS))"; \
	elif [[ ! "$(SKIP_SECRET)" =~ ^(1|true|yes)$$ ]]; then \
		$(MAKE) --no-print-directory secret MODEL="$(firstword $(MODELS))"; \
	fi

deploy-all-releases: $(DEPLOY_ALL_RELEASE_TARGETS)

$(DEPLOY_ALL_RELEASE_TARGETS): deploy-all-release-%:
	@echo "==> deploy $*"
	@$(MAKE) --no-print-directory upgrade MODEL="$*"
	@if [[ "$(PLANNER_COMPAT)" =~ ^(1|true|yes)$$ ]]; then \
		$(MAKE) --no-print-directory planner-compat MODEL="$*"; \
	fi

deploy-all-routes: $(DEPLOY_ALL_ROUTE_TARGETS)

$(DEPLOY_ALL_ROUTE_TARGETS): deploy-all-route-%:
	@$(MAKE) --no-print-directory gateway-route-apply MODEL="$*"

uninstall-all:
	@for model in $(MODELS); do \
		echo "==> uninstall $$model"; \
		$(MAKE) --no-print-directory uninstall MODEL="$$model"; \
	done

.PHONY: build-% push-% deploy-% upgrade-% install-% render-% template-% lint-% dry-run-% uninstall-% delete-% status-% history-% gateway-%
build-%:
	@$(MAKE) --no-print-directory build MODEL="$*"

push-%:
	@$(MAKE) --no-print-directory push MODEL="$*"

deploy-%:
	@$(MAKE) --no-print-directory deploy MODEL="$*"

upgrade-% install-%:
	@$(MAKE) --no-print-directory upgrade MODEL="$*"

render-% template-%:
	@$(MAKE) --no-print-directory render MODEL="$*"

lint-%:
	@$(MAKE) --no-print-directory lint MODEL="$*"

dry-run-%:
	@$(MAKE) --no-print-directory dry-run MODEL="$*"

uninstall-% delete-%:
	@$(MAKE) --no-print-directory uninstall MODEL="$*"

status-%:
	@$(MAKE) --no-print-directory status MODEL="$*"

history-%:
	@$(MAKE) --no-print-directory history MODEL="$*"

gateway-%:
	@$(MAKE) --no-print-directory gateway MODEL="$*"
