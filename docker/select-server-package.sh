#!/bin/sh
# Print the workspace package the image installs, from KIDSPLAY_WITH_YTDLP.
#
# Used by the Dockerfile at build time. Common spellings of "yes" and "no" are
# accepted, in any case; anything else fails the build. Silently building a
# core-only image for a value like "true" leaves the operator without YouTube
# import and no hint why.
set -eu

value=$(printf '%s' "${KIDSPLAY_WITH_YTDLP:-}" | tr '[:upper:]' '[:lower:]')
case "$value" in
    1 | true | yes | on) echo kidsplay-importer-ytdlp ;;
    0 | false | no | off) echo kidsplay-server ;;
    *)
        echo "KIDSPLAY_WITH_YTDLP must be 1/true/yes/on or 0/false/no/off," \
            "not '${KIDSPLAY_WITH_YTDLP:-}'" >&2
        exit 1
        ;;
esac
