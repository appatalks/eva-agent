#!/usr/bin/env bash
# Shared Linux AppImage integration for the local and remote installers.
#
# This file intentionally contains only small shell helpers.  The launcher
# resolves the highest stable semantic-version AppImage when it is run instead
# of pinning a release path, because AppImageUpdate may install a newer version
# beside the old one.

eva_appimage_list() {
  local dist_dir="${1:-}"
  [ -d "$dist_dir" ] || return 1
  find "$dist_dir" -maxdepth 1 -type f -perm -111 \
    \( -name 'Eva Standalone-*.AppImage' -o -name 'Eva.Standalone-*.AppImage' \) \
    -printf '%T@ %p\n' 2>/dev/null |
    LC_ALL=C sort -nr
}

eva_appimage_find_newest() {
  local dist_dir="${1:-}"
  local record appimage version best_version="" best_appimage=""
  [ -d "$dist_dir" ] || return 1

  # eva_appimage_list is mtime-descending, so equal versions naturally prefer
  # the newest file.  Stable releases are deliberately restricted to
  # MAJOR.MINOR.PATCH; prereleases are not selected over a stable release.
  while IFS= read -r record; do
    appimage="${record#* }"
    version="$(basename "$appimage")"
    case "$version" in
      "Eva Standalone-"*.AppImage) version="${version#Eva Standalone-}" ;;
      "Eva.Standalone-"*.AppImage) version="${version#Eva.Standalone-}" ;;
      *) continue ;;
    esac
    version="${version%.AppImage}"
    [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || continue
    if [ -z "$best_version" ] || {
      [ "$version" != "$best_version" ] &&
      [ "$(printf '%s\n%s\n' "$best_version" "$version" | LC_ALL=C sort -V | tail -n 1)" = "$version" ];
    }; then
      best_version="$version"
      best_appimage="$appimage"
    fi
  done < <(eva_appimage_list "$dist_dir" || true)

  if [ -n "$best_appimage" ]; then
    printf '%s\n' "$best_appimage"
  else
    # Keep a useful fallback for locally named prereleases or development
    # artifacts when no stable semantic-version file is present.
    eva_appimage_list "$dist_dir" | sed -e 's/^[^ ]* //' -e '1q'
  fi
}

eva_appimage_zsync_path() {
  local appimage="$1"
  local directory basename
  directory="$(dirname "$appimage")"
  basename="$(basename "$appimage" | tr ' ' '.')"
  printf '%s/%s.zsync' "$directory" "$basename"
}

eva_desktop_quote() {
  # Desktop Exec fields use quoting rules distinct from shell quoting.
  # The launcher path is one argument, so quote it and escape characters that
  # desktop files treat specially inside double quotes.
  local value="$1"
  value="$(printf '%s' "$value" | sed \
    -e 's/\\/\\\\/g' \
    -e 's/"/\\"/g' \
    -e 's/`/\\`/g' \
    -e 's/\$/\\$/g')"
  printf '"%s"' "$value"
}

eva_desktop_value_escape() {
  # Non-Exec desktop keys use the Desktop Entry string escapes, not quotes.
  printf '%s' "$1" | sed \
    -e 's/\\/\\\\/g' \
    -e 's/ /\\s/g' \
    -e 's/	/\\t/g'
}

eva_write_appimage_launcher() {
  local install_dir="$1"
  local launcher="$2"
  mkdir -p "$(dirname "$launcher")"
  {
    printf '%s\n' '#!/usr/bin/env bash'
    printf '%s\n' 'set -euo pipefail'
    printf 'EVA_HOME_DEFAULT=%q\n' "$install_dir"
    printf '%s\n' 'EVA_HOME="${EVA_HOME:-$EVA_HOME_DEFAULT}"'
    printf '%s\n' 'DIST_DIR="$EVA_HOME/standalone/dist"'
    printf '%s\n' 'LINUX_INTEGRATION="$EVA_HOME/standalone/linux-integration.sh"'
    cat <<'EVA_LAUNCHER'
if [ ! -f "$LINUX_INTEGRATION" ]; then
  printf 'Eva Linux integration helper was not found under %s\n' "$EVA_HOME" >&2
  exit 1
fi
# shellcheck source=standalone/linux-integration.sh
. "$LINUX_INTEGRATION"
appimage="$(
  eva_appimage_find_newest "$DIST_DIR" || true
)"
if [ -z "$appimage" ] || [ ! -x "$appimage" ]; then
  printf 'Eva AppImage was not found under %s\n' "$DIST_DIR" >&2
  exit 1
fi
exec "$appimage" --eva-workspace-terminal-v1 "$@"
EVA_LAUNCHER
  } > "$launcher"
  chmod 755 "$launcher"
}

eva_write_desktop_entry() {
  local desktop="$1"
  local launcher="$2"
  local icon="$3"
  mkdir -p "$(dirname "$desktop")"
  {
    printf '%s\n' '[Desktop Entry]'
    printf '%s\n' 'Type=Application'
    printf '%s\n' 'Name=Eva'
    printf '%s\n' 'Comment=Eva AI Assistant'
    printf 'Exec='
    eva_desktop_quote "$launcher"
    printf '\n'
    printf 'Icon='
    eva_desktop_value_escape "$icon"
    printf '\n'
    printf '%s\n' 'Terminal=false'
    printf '%s\n' 'Categories=Utility;ArtificialIntelligence;'
    printf '%s\n' 'StartupWMClass=Eva'
  } > "$desktop"
  chmod 644 "$desktop"
}

eva_refresh_system_integration() {
  local install_dir="$1"
  local dist_dir="$install_dir/standalone/dist"
  local launcher="$HOME/.local/bin/eva"
  local desktop="$HOME/.local/share/applications/eva.desktop"
  local icon="$install_dir/core/img/eva-icon.svg"
  local appimage

  appimage="$(eva_appimage_find_newest "$dist_dir" || true)"
  [ -n "$appimage" ] && [ -x "$appimage" ] || {
    printf '%s\n' "Eva AppImage was not found; launcher was not refreshed." >&2
    return 1
  }
  eva_write_appimage_launcher "$install_dir" "$launcher"
  eva_write_desktop_entry "$desktop" "$launcher" "$icon"
  printf '%s\n' "System launcher refreshed: $launcher (current AppImage: $appimage)"
}
