#!/usr/bin/env bash
# Image-tag selection and collision-safe publishing for build_mcp.yml.
#
# The ECR repository uses immutable tags, so a tag can only ever be pushed
# once. This script picks a tag that is not already in use (looking at both the
# git `v*` tags and the tags already present in ECR) and pushes it, handling
# only ECR's explicit "tag already exists ... immutable" rejection.
#
# Tag conventions:
#   main       -> <X.Y.Z>                 next free semver; git tag v<X.Y.Z>
#   staging    -> stag-<X.Y.Z>-<sha7>     preview of the next release + commit
#   feature/*  -> <TICKET>-<sha7>         e.g. APLF-664-1a2b3c4
#
# Sub-commands (all *_file inputs are plain files with one tag per line):
#   next-version  <git_tags_file> <ecr_tags_file>
#   resolve       <branch> <sha> <git_tags_file> <head_tags_file> <ecr_tags_file>
#                 Prints `tag=<tag>` and `skip=<true|false>` (GITHUB_OUTPUT
#                 format). skip=true means this commit's image is already in ECR.
#   publish       <branch> <image_repo> <local_image> <tag> <git_tags_file> <ecr_tags_file>
#                 Pushes <image_repo>:<tag>; on an immutable-tag collision for
#                 `main` it moves to the next free version and retries. Prints
#                 `tag=<published tag>` and `pushed=<true|false>`.
#   is-collision  <tag> <push_log_file>
#                 Exit 0 if the log is ECR's immutable-tag rejection for <tag>.
set -euo pipefail

SEMVER_CORE='(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)'
MAX_PUSH_ATTEMPTS="${MAX_PUSH_ATTEMPTS:-5}"
PUSH_LOG=""
trap '[[ -z "$PUSH_LOG" ]] || rm -f "$PUSH_LOG"' EXIT

die() {
  echo "::error::$*" >&2
  exit 1
}

# Release versions from git tags: only exact `vX.Y.Z` (leading `v` stripped).
git_versions() {
  grep -E "^v${SEMVER_CORE}$" "$1" | sed 's/^v//' || true
}

# Release versions from ECR tags: only exact `X.Y.Z`.
ecr_versions() {
  grep -E "^${SEMVER_CORE}$" "$1" || true
}

# Highest X.Y.Z read from stdin, compared numerically per component.
max_version() {
  sort -u -t. -k1,1n -k2,2n -k3,3n | tail -n1
}

next_version() {
  local git_file=$1 ecr_file=$2 latest major minor patch
  latest=$({ git_versions "$git_file"; ecr_versions "$ecr_file"; } | max_version)
  latest="${latest:-0.0.0}"
  IFS='.' read -r major minor patch <<<"$latest"
  echo "${major}.${minor}.$((patch + 1))"
}

# `feature/APLF-664-some-description` -> `APLF-664`.
ticket_from_branch() {
  local branch=$1 ticket
  ticket=$(sed -nE 's/^feature\/([A-Za-z0-9-]+)-([0-9]+).*/\1-\2/p' <<<"$branch")
  [[ -n "$ticket" ]] || die "Cannot extract a ticket number from '$branch' (expected feature/<PROJECT>-<NUMBER>...)."
  echo "$ticket"
}

short_sha() {
  local sha=$1
  [[ "$sha" =~ ^[0-9a-f]{7,40}$ ]] || die "Invalid commit SHA '$sha'."
  echo "${sha:0:7}"
}

has_line() {
  grep -qxF -- "$1" "$2"
}

resolve() {
  local branch=$1 sha=$2 git_file=$3 head_file=$4 ecr_file=$5
  local sha7 tag version existing

  case "$branch" in
    main)
      # Rerun of an already released commit: reuse its `vX.Y.Z` tag. If the
      # image is in ECR there is nothing to do; otherwise publish under it.
      version=$(git_versions "$head_file" | max_version)
      if [[ -n "$version" ]]; then
        if has_line "$version" "$ecr_file"; then
          printf 'tag=%s\nskip=true\n' "$version"
        else
          printf 'tag=%s\nskip=false\n' "$version"
        fi
        return
      fi
      printf 'tag=%s\nskip=false\n' "$(next_version "$git_file" "$ecr_file")"
      ;;
    staging)
      sha7=$(short_sha "$sha")
      existing=$(grep -E "^stag-${SEMVER_CORE}-${sha7}$" "$ecr_file" | head -n1 || true)
      if [[ -n "$existing" ]]; then
        printf 'tag=%s\nskip=true\n' "$existing"
        return
      fi
      printf 'tag=%s\nskip=false\n' "stag-$(next_version "$git_file" "$ecr_file")-${sha7}"
      ;;
    feature/*)
      sha7=$(short_sha "$sha")
      tag="$(ticket_from_branch "$branch")-${sha7}"
      if has_line "$tag" "$ecr_file"; then
        printf 'tag=%s\nskip=true\n' "$tag"
      else
        printf 'tag=%s\nskip=false\n' "$tag"
      fi
      ;;
    *)
      die "Unsupported branch '$branch'. Build failed."
      ;;
  esac
}

# Matches only ECR's explicit immutable-tag rejection for this exact tag, e.g.
#   tag invalid: The image tag '0.0.2' already exists in the 'phs/apeiron-mcp'
#   repository and cannot be overwritten because the tag is immutable.
is_collision() {
  local tag=$1 log=$2
  grep -qF "The image tag '${tag}' already exists" "$log" &&
    grep -qF "because the tag is immutable" "$log"
}

publish() {
  local branch=$1 image_repo=$2 local_image=$3 tag=$4 git_file=$5 ecr_file=$6
  local attempt rc
  PUSH_LOG=$(mktemp)

  for ((attempt = 1; attempt <= MAX_PUSH_ATTEMPTS; attempt++)); do
    echo "Pushing ${image_repo}:${tag} (attempt ${attempt}/${MAX_PUSH_ATTEMPTS})" >&2
    docker tag "$local_image" "${image_repo}:${tag}"
    rc=0
    docker push "${image_repo}:${tag}" 2>&1 | tee "$PUSH_LOG" >&2 || rc=$?
    if [[ $rc -eq 0 ]]; then
      printf 'tag=%s\npushed=true\n' "$tag"
      return 0
    fi
    if ! is_collision "$tag" "$PUSH_LOG"; then
      die "docker push of '${tag}' failed (exit ${rc}) for a reason other than an immutable-tag collision."
    fi

    case "$branch" in
      main)
        # Someone else published this version meanwhile; claim the next one.
        echo "::warning::Image tag '${tag}' already exists in ECR (immutable); selecting the next version." >&2
        printf '\n%s\n' "$tag" >>"$ecr_file"
        tag=$(next_version "$git_file" "$ecr_file")
        ;;
      *)
        # Commit-scoped tag: this commit's image has already been published.
        echo "::warning::Image tag '${tag}' already exists in ECR for this commit; nothing to push." >&2
        printf 'tag=%s\npushed=false\n' "$tag"
        return 0
        ;;
    esac
  done
  die "Could not find a free image tag after ${MAX_PUSH_ATTEMPTS} attempts."
}

main() {
  local cmd=${1:-}
  shift || true
  case "$cmd" in
    next-version)
      [[ $# -eq 2 ]] || die "usage: $0 next-version <git_tags_file> <ecr_tags_file>"
      next_version "$@"
      ;;
    resolve)
      [[ $# -eq 5 ]] || die "usage: $0 resolve <branch> <sha> <git_tags_file> <head_tags_file> <ecr_tags_file>"
      resolve "$@"
      ;;
    publish)
      [[ $# -eq 6 ]] || die "usage: $0 publish <branch> <image_repo> <local_image> <tag> <git_tags_file> <ecr_tags_file>"
      publish "$@"
      ;;
    is-collision)
      [[ $# -eq 2 ]] || die "usage: $0 is-collision <tag> <push_log_file>"
      is_collision "$@"
      ;;
    *)
      die "usage: $0 {next-version|resolve|publish|is-collision} ..."
      ;;
  esac
}

main "$@"
