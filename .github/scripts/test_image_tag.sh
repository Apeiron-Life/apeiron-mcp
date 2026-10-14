#!/usr/bin/env bash
# Tests for image_tag.sh. Run: bash .github/scripts/test_image_tag.sh
# `docker` is replaced by a stub on PATH, so no registry access is needed.
set -euo pipefail

SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/image_tag.sh"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

PASS=0
FAIL=0
SHA=1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d

check() {
  local name=$1 expected=$2 actual=$3
  if [[ "$expected" == "$actual" ]]; then
    PASS=$((PASS + 1))
  else
    FAIL=$((FAIL + 1))
    printf 'FAIL: %s\n  expected: %q\n  actual:   %q\n' "$name" "$expected" "$actual"
  fi
}

lines() {
  local file="$WORK/$1"
  shift
  : >"$file"
  local l
  for l in "$@"; do echo "$l" >>"$file"; done
  echo "$file"
}

# --- next-version -----------------------------------------------------------
EMPTY=$(lines empty)
check "no tags anywhere" "0.0.1" "$("$SCRIPT" next-version "$EMPTY" "$EMPTY")"

# The original failure: git has v0.0.1 but ECR already holds 0.0.2.
GIT=$(lines git v0.0.1)
ECR=$(lines ecr 0.0.1 0.0.2 APLF-1 stag-0.0.2)
check "ECR ahead of git" "0.0.3" "$("$SCRIPT" next-version "$GIT" "$ECR")"

GIT=$(lines git v0.0.9 v0.0.10 v0.0.2)
check "numeric (not lexical) sort" "0.0.11" "$("$SCRIPT" next-version "$GIT" "$EMPTY")"

GIT=$(lines git v0.9.9 v1.0.0 v0.10.0)
check "major/minor ordering" "1.0.1" "$("$SCRIPT" next-version "$GIT" "$EMPTY")"

GIT=$(lines git v0.0.3 v9.9.9-rc1 v99 release-1 v01.0.0 V5.0.0 v1.2 v0.0.3.1 foo)
ECR=$(lines ecr stag-7.0.0 8.0.0-beta APLF-664 latest)
check "non-semver tags ignored" "0.0.4" "$("$SCRIPT" next-version "$GIT" "$ECR")"

GIT=$(lines git v0.0.5)
ECR=$(lines ecr 0.0.2)
check "git ahead of ECR" "0.0.6" "$("$SCRIPT" next-version "$GIT" "$ECR")"

# --- resolve ----------------------------------------------------------------
GIT=$(lines git v0.0.1)
ECR=$(lines ecr 0.0.1 0.0.2)
HEAD_NONE=$(lines head_none)
check "main: next free version" $'tag=0.0.3\nskip=false' \
  "$("$SCRIPT" resolve main "$SHA" "$GIT" "$HEAD_NONE" "$ECR")"

HEAD=$(lines head v0.0.2)
GIT=$(lines git v0.0.1 v0.0.2)
check "main: rerun of released commit is skipped" $'tag=0.0.2\nskip=true' \
  "$("$SCRIPT" resolve main "$SHA" "$GIT" "$HEAD" "$ECR")"

ECR_ONLY1=$(lines ecr_only1 0.0.1)
check "main: tagged commit without image publishes its own version" $'tag=0.0.2\nskip=false' \
  "$("$SCRIPT" resolve main "$SHA" "$GIT" "$HEAD" "$ECR_ONLY1")"

HEAD_JUNK=$(lines head_junk some-label v1.0)
check "main: non-semver head tags ignored" $'tag=0.0.3\nskip=false' \
  "$("$SCRIPT" resolve main "$SHA" "$GIT" "$HEAD_JUNK" "$ECR")"

check "staging: commit-unique tag" $'tag=stag-0.0.3-1a2b3c4\nskip=false' \
  "$("$SCRIPT" resolve staging "$SHA" "$GIT" "$HEAD_NONE" "$ECR")"

ECR_STAG=$(lines ecr_stag 0.0.1 0.0.2 0.0.3 stag-0.0.3-1a2b3c4)
check "staging: rerun of published commit is skipped" $'tag=stag-0.0.3-1a2b3c4\nskip=true' \
  "$("$SCRIPT" resolve staging "$SHA" "$GIT" "$HEAD_NONE" "$ECR_STAG")"

check "feature: ticket + sha" $'tag=APLF-664-1a2b3c4\nskip=false' \
  "$("$SCRIPT" resolve feature/APLF-664-add-thing "$SHA" "$GIT" "$HEAD_NONE" "$ECR")"

ECR_FEAT=$(lines ecr_feat APLF-664 APLF-664-1a2b3c4)
check "feature: rerun of published commit is skipped" $'tag=APLF-664-1a2b3c4\nskip=true' \
  "$("$SCRIPT" resolve feature/APLF-664-add-thing "$SHA" "$GIT" "$HEAD_NONE" "$ECR_FEAT")"

check "feature: branch without ticket fails" "fail" \
  "$("$SCRIPT" resolve feature/no-ticket "$SHA" "$GIT" "$HEAD_NONE" "$ECR" 2>/dev/null && echo ok || echo fail)"
check "unsupported branch fails" "fail" \
  "$("$SCRIPT" resolve dev "$SHA" "$GIT" "$HEAD_NONE" "$ECR" 2>/dev/null && echo ok || echo fail)"
check "invalid sha fails" "fail" \
  "$("$SCRIPT" resolve staging 'abc;rm' "$GIT" "$HEAD_NONE" "$ECR" 2>/dev/null && echo ok || echo fail)"

# --- is-collision -----------------------------------------------------------
COLLISION_LOG=$(lines collision_log \
  "15f1c8eb1ab1: Pushed" \
  "tag invalid: The image tag '0.0.2' already exists in the 'phs/apeiron-mcp' repository and cannot be overwritten because the tag is immutable.")
check "collision detected" "yes" \
  "$("$SCRIPT" is-collision 0.0.2 "$COLLISION_LOG" && echo yes || echo no)"
check "collision for another tag not matched" "no" \
  "$("$SCRIPT" is-collision 0.0.3 "$COLLISION_LOG" && echo yes || echo no)"
OTHER_LOG=$(lines other_log "denied: Your authorization token has expired. Reauthenticate and try again.")
check "unrelated error is not a collision" "no" \
  "$("$SCRIPT" is-collision 0.0.2 "$OTHER_LOG" && echo yes || echo no)"

# --- publish (stubbed docker) -----------------------------------------------
# DOCKER_EXISTING: space-separated tags the fake registry already holds.
# DOCKER_FAIL_OTHER=1: every push fails with an unrelated error.
STUB="$WORK/bin"
mkdir -p "$STUB"
cat >"$STUB/docker" <<'EOF'
#!/usr/bin/env bash
[[ "$1" == "tag" ]] && exit 0
ref=$2
tag=${ref##*:}
echo "push $tag" >>"$DOCKER_CALLS"
if [[ "${DOCKER_FAIL_OTHER:-0}" == 1 ]]; then
  echo "denied: Your authorization token has expired."
  exit 1
fi
for t in $DOCKER_EXISTING; do
  if [[ "$t" == "$tag" ]]; then
    echo "tag invalid: The image tag '$tag' already exists in the 'phs/apeiron-mcp' repository and cannot be overwritten because the tag is immutable."
    exit 1
  fi
done
echo "$tag: digest: sha256:abc size: 1234"
EOF
chmod +x "$STUB/docker"
export PATH="$STUB:$PATH" DOCKER_CALLS="$WORK/calls"

run_publish() {
  : >"$DOCKER_CALLS"
  "$SCRIPT" publish "$@" 2>/dev/null
}

GIT=$(lines git v0.0.1)
ECR=$(lines ecr 0.0.1)
check "publish: free tag pushed" $'tag=0.0.2\npushed=true' \
  "$(DOCKER_EXISTING="" run_publish main repo/img local:build 0.0.2 "$GIT" "$ECR")"

ECR=$(lines ecr 0.0.1)
check "publish main: race on 0.0.2 and 0.0.3 -> 0.0.4" $'tag=0.0.4\npushed=true' \
  "$(DOCKER_EXISTING="0.0.2 0.0.3" run_publish main repo/img local:build 0.0.2 "$GIT" "$ECR")"
check "publish main: retried exactly the colliding tags" $'push 0.0.2\npush 0.0.3\npush 0.0.4' \
  "$(cat "$DOCKER_CALLS")"

ECR=$(lines ecr 0.0.1)
check "publish main: gives up after MAX_PUSH_ATTEMPTS" "fail" \
  "$(MAX_PUSH_ATTEMPTS=2 DOCKER_EXISTING="0.0.2 0.0.3 0.0.4" run_publish main repo/img local:build 0.0.2 "$GIT" "$ECR" && echo ok || echo fail)"

ECR=$(lines ecr 0.0.1)
check "publish: unrelated push error fails (not treated as collision)" "fail" \
  "$(DOCKER_FAIL_OTHER=1 run_publish main repo/img local:build 0.0.2 "$GIT" "$ECR" && echo ok || echo fail)"
check "publish: unrelated push error is not retried" "push 0.0.2" "$(cat "$DOCKER_CALLS")"

ECR=$(lines ecr)
check "publish feature: existing commit tag -> already published" $'tag=APLF-664-1a2b3c4\npushed=false' \
  "$(DOCKER_EXISTING="APLF-664-1a2b3c4" run_publish feature/APLF-664-x repo/img local:build APLF-664-1a2b3c4 "$GIT" "$ECR")"

echo "passed: $PASS, failed: $FAIL"
[[ $FAIL -eq 0 ]]
