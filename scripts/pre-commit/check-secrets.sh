#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
source scripts/pre-commit/common.sh

status=0
secret_re='(HF_TOKEN|NGC_API_KEY|NVIDIA_API_KEY|OPENAI_API_KEY|AWS_SECRET_ACCESS_KEY|PHOTO_TAGGER_AUTH_TOKEN|POSTGRES_PASSWORD|REGISTRY_PASSWORD)[[:space:]]*[:=][[:space:]]*["'\'']?[A-Za-z0-9_./+=:@-]{12,}'

for file in "$@"; do
  [[ -f "${file}" ]] || continue
  is_text_file "${file}" || continue

  case "${file}" in
    *.md|PRE_COMMIT.md|AGENTS.md)
      # Documentation intentionally contains placeholder variable names.
      ;;
    *)
      if grep -nE "${secret_re}" "${file}" > /dev/null; then
        echo "Possible committed credential in ${file}" >&2
        status=1
      fi
      ;;
  esac

  if grep -nE -- '-----BEGIN [A-Z ]*PRIVATE KEY-----' "${file}" > /dev/null; then
    echo "Private key material found in ${file}" >&2
    status=1
  fi

  if [[ "${file}" =~ \.ya?ml$ ]] && grep -qE '^kind:[[:space:]]*Secret$' "${file}"; then
    echo "Kubernetes Secret manifest found in ${file}; create secrets at deploy time instead" >&2
    status=1
  fi

  if grep -qE '(^current-context:|^client-key-data:|^client-certificate-data:)' "${file}"; then
    echo "Possible kubeconfig content found in ${file}" >&2
    status=1
  fi
done

exit "${status}"
