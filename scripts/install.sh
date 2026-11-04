#!/usr/bin/env bash
# Update an already provisioned K20 Pro server installation.
# NOTE: Unsafe legacy file synchronization, unjournaled in-place copies,
# and ambient supervisor restarts have been completely replaced by the
# reviewed transactional activation adapter (scripts/release_activation.py)
# and release transaction engine (scripts/release_transaction.py).
#
# This wrapper parses CLI arguments strictly and delegates directly to the
# reviewed activation adapter. It never performs ad-hoc source writes,
# service manipulation, or Caddy reloads directly.
set -euo pipefail

# Fail-fast guard: a no-argument invocation must fail immediately before any
# external commands or path resolution are executed.
if [ "$#" -eq 0 ]; then
    echo "Direct source writes via legacy installer are disabled: reviewed transactional activation adapter is required." >&2
    exit 1
fi

action="$1"
shift

case "$action" in
    plan|apply)
        ;;
    *)
        echo "Error: invalid action '$action'. Expected 'plan' or 'apply'." >&2
        exit 1
        ;;
esac

bundle=""
preconditions=""
transaction_dir=""
namespace_root=""

has_bundle=0
has_preconditions=0
has_transaction_dir=0
has_namespace_root=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        --bundle)
            if [ "$has_bundle" -ne 0 ]; then
                echo "Error: duplicate flag --bundle" >&2
                exit 1
            fi
            if [ "$#" -lt 2 ] || [ -z "$2" ]; then
                echo "Error: missing or empty value for --bundle" >&2
                exit 1
            fi
            bundle="$2"
            has_bundle=1
            shift 2
            ;;
        --preconditions)
            if [ "$has_preconditions" -ne 0 ]; then
                echo "Error: duplicate flag --preconditions" >&2
                exit 1
            fi
            if [ "$#" -lt 2 ] || [ -z "$2" ]; then
                echo "Error: missing or empty value for --preconditions" >&2
                exit 1
            fi
            preconditions="$2"
            has_preconditions=1
            shift 2
            ;;
        --transaction-dir)
            if [ "$action" != "apply" ]; then
                echo "Error: flag --transaction-dir is only allowed for 'apply'" >&2
                exit 1
            fi
            if [ "$has_transaction_dir" -ne 0 ]; then
                echo "Error: duplicate flag --transaction-dir" >&2
                exit 1
            fi
            if [ "$#" -lt 2 ] || [ -z "$2" ]; then
                echo "Error: missing or empty value for --transaction-dir" >&2
                exit 1
            fi
            transaction_dir="$2"
            has_transaction_dir=1
            shift 2
            ;;
        --namespace-root)
            if [ "$has_namespace_root" -ne 0 ]; then
                echo "Error: duplicate flag --namespace-root" >&2
                exit 1
            fi
            if [ "$#" -lt 2 ] || [ -z "$2" ]; then
                echo "Error: missing or empty value for --namespace-root" >&2
                exit 1
            fi
            namespace_root="$2"
            has_namespace_root=1
            shift 2
            ;;
        *)
            echo "Error: unknown or unsupported argument '$1'" >&2
            exit 1
            ;;
    esac
done

if [ "$has_bundle" -eq 0 ]; then
    echo "Error: missing mandatory flag --bundle" >&2
    exit 1
fi

if [ "$has_preconditions" -eq 0 ]; then
    echo "Error: missing mandatory flag --preconditions" >&2
    exit 1
fi

if [ "$action" = "apply" ] && [ "$has_transaction_dir" -eq 0 ]; then
    echo "Error: missing mandatory flag --transaction-dir for 'apply'" >&2
    exit 1
fi

# Locate sibling activation script using safe script-relative path resolution
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
activation_script="$script_dir/release_activation.py"

if [ ! -f "$activation_script" ]; then
    echo "Error: reviewed release activation adapter not found: $activation_script" >&2
    echo "This branch requires root integration of the accepted release activation adapter before execution." >&2
    exit 1
fi

# Build argv array and exec Python CLI directly
cmd=(python3 "$activation_script" "$action" --bundle "$bundle" --preconditions "$preconditions")

if [ "$action" = "apply" ]; then
    cmd+=(--transaction-dir "$transaction_dir")
fi

if [ "$has_namespace_root" -ne 0 ]; then
    cmd+=(--namespace-root "$namespace_root")
fi

exec "${cmd[@]}"
