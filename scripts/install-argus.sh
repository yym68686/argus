#!/usr/bin/env bash
set -euo pipefail

REPO="${ARGUS_GITHUB_REPO:-yym68686/argus}"
REF="${ARGUS_GITHUB_REF:-main}"
VERSION="${ARGUS_VERSION:-}"
BIN_DIR="${ARGUS_BIN_DIR:-${HOME}/.argus/bin}"
BIN_PATH="${BIN_DIR}/argus"
LINK_PATH="${ARGUS_LINK_PATH:-/usr/local/bin/argus}"

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "$1 is required" >&2
    exit 1
  fi
}

download() {
  local url="$1"
  local out="$2"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$url" -o "$out"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$out" "$url"
  else
    echo "curl or wget is required" >&2
    exit 1
  fi
}

detect_target() {
  local os arch
  os="$(uname -s 2>/dev/null | tr '[:upper:]' '[:lower:]' || echo unknown)"
  arch="$(uname -m 2>/dev/null || echo unknown)"
  case "${os}:${arch}" in
    darwin:arm64|darwin:aarch64) echo "darwin-arm64" ;;
    darwin:x86_64|darwin:amd64) echo "darwin-amd64" ;;
    linux:arm64|linux:aarch64) echo "linux-arm64" ;;
    linux:x86_64|linux:amd64) echo "linux-amd64" ;;
    *)
      echo "Unsupported platform: ${os}/${arch}" >&2
      exit 1
      ;;
  esac
}

install_link() {
  if [ "${ARGUS_INSTALL_SYSTEM_LINK:-1}" = "0" ]; then
    return 0
  fi
  local link_dir
  link_dir="$(dirname "$LINK_PATH")"
  if [ ! -d "$link_dir" ]; then
    if command -v sudo >/dev/null 2>&1; then
      sudo mkdir -p "$link_dir"
    else
      mkdir -p "$link_dir"
    fi
  fi
  if [ -w "$link_dir" ]; then
    ln -sf "$BIN_PATH" "$LINK_PATH"
  elif command -v sudo >/dev/null 2>&1; then
    sudo ln -sf "$BIN_PATH" "$LINK_PATH"
  else
    echo "Unable to create $LINK_PATH; add $BIN_DIR to PATH to run argus anywhere." >&2
  fi
}

install_from_go() {
  need_cmd go
  local tmp
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  git_url="https://github.com/${REPO}.git"
  if command -v git >/dev/null 2>&1; then
    git clone --depth 1 --branch "$REF" "$git_url" "$tmp/repo"
  else
    download "https://github.com/${REPO}/archive/${REF}.tar.gz" "$tmp/src.tar.gz"
    mkdir -p "$tmp/repo"
    tar -xzf "$tmp/src.tar.gz" -C "$tmp/repo" --strip-components 1
  fi
  (cd "$tmp/repo/apps/node-host" && go build -trimpath -ldflags "-s -w" -o "$BIN_PATH" ./cmd/argus)
}

mkdir -p "$BIN_DIR"
target="$(detect_target)"

if [ -z "$VERSION" ]; then
  version_url="https://raw.githubusercontent.com/${REPO}/${REF}/VERSION"
  tmp_version="$(mktemp)"
  if download "$version_url" "$tmp_version"; then
    VERSION="$(tr -d '[:space:]' < "$tmp_version")"
  fi
  rm -f "$tmp_version"
fi

installed=0
if [ -n "$VERSION" ]; then
  release_url="https://github.com/${REPO}/releases/download/v${VERSION}/argus-${target}"
  tmp_bin="$(mktemp)"
  if download "$release_url" "$tmp_bin"; then
    install -m 0755 "$tmp_bin" "$BIN_PATH"
    installed=1
  fi
  rm -f "$tmp_bin"
fi

if [ "$installed" != "1" ]; then
  echo "GitHub release binary was not available; building argus from source with Go." >&2
  install_from_go
  chmod +x "$BIN_PATH"
fi

install_link

if command -v argus >/dev/null 2>&1; then
  argus_path="$(command -v argus)"
else
  argus_path="$BIN_PATH"
fi

echo "Installed argus CLI: $argus_path"
"$argus_path" --help >/dev/null
