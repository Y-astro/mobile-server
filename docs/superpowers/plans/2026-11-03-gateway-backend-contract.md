# Gateway Backend Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement the bounded task, followed by independent review.

**Goal:** Rebuild the gateway from maintained source so it starts with the existing FileBrowser backend on 127.0.0.1:8085, while retaining legacy 8080 compatibility and loopback restrictions.

**Architecture:** Replace the port-specific startup assertion with one small parsed-URL validator permitting the two known loopback endpoints. Keep the proxy, authentication, admission and streaming code unchanged.

**Tech Stack:** Go standard library, existing gateway race tests, local build and startup smoke checks.

**Spec:** docs/superpowers/specs/2026-11-03-redmi-server-stabilization-design.md

## Global Constraints

- Preserve user data, service behavior, upstream authentication and streaming/admission limits.
- Keep SSH available and disabled services disabled.
- No phone writes, restarts, deployments, credential access or package installs in this task.
- Only edit gateway/main.go and gateway/main_test.go. This branch is separate from the concurrently running dashboard repair worker, as permitted by the approved design for independent worktrees.

### Task 1: Support the existing FileBrowser backend in source

**Files:** Modify gateway/main.go and gateway/main_test.go.

**Interfaces:** parseBackend(raw string) (*url.URL, error) parses/validates the startup backend. main uses this function. The default backend becomes http://127.0.0.1:8085. Explicit http://127.0.0.1:8080 remains accepted. Existing URL path/query behavior need not change; preserve scheme/host validation semantics except the approved additional port.

- [ ] Add table-driven validator tests for both accepted backend ports, malformed URLs, HTTPS, nonloopback IP/hostname, and unrelated loopback ports.
- [ ] Run focused tests and record failure against old source.
- [ ] Add parseBackend and wire main to it. The validation predicate is:

```go
u, err := url.Parse(raw)
if err != nil || u.Scheme != "http" || (u.Host != "127.0.0.1:8080" && u.Host != "127.0.0.1:8085") {
    return nil, fmt.Errorf("backend must be http://127.0.0.1:8085 or http://127.0.0.1:8080")
}
return u, nil
```

Do not broaden accepted backends to arbitrary hosts, ports or schemes. Keep listen loopback validation intact.

- [ ] Run gofmt on changed Go files and go test -race ./... from gateway/.
- [ ] Build a temporary native gateway under ignored .superpowers/sdd; start it on an ephemeral loopback listener with -backend http://127.0.0.1:8085, probe /healthz and terminate only that exact child. A dummy backend server is permitted on local host if needed; no real phone APIs. Also verify unsupported backend port exits before serving.
- [ ] Crossbuild ARM64 twice with GOTOOLCHAIN=local GOOS=linux GOARCH=arm64 CGO_ENABLED=0 go build -trimpath -buildvcs=false, writing ignored artifacts. Verify both SHA-256 hashes match; record toolchain and exact flags. No source/binary byte patching.
- [ ] Run git diff --check, commit only the two changed Go source/test files, and write a concise implementation report with actual commands/results/commits. Do not deploy or modify current phone binary.

Acceptance: both known backend ports validate; source-built process starts with 8085; unsupported backend rejected; existing race suite passes; deterministic ARM64 artifacts produced; upstream proxy/admission logic unchanged.
