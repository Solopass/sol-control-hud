#!/usr/bin/env bash
# Serve OmniTools from a production build (the dashboard's Start button runs this inside WSL).
#
# `npm run dev` recompiles the whole app on every start (20+ s cold); serving the built files with `vite preview`
# starts in a second or two and uses less memory. The build (~1.5 min) runs only when OmniTools' code changed since
# the last one: the stamp is the commit plus a hash of uncommitted changes. If a build fails, the previous one is
# served; with no build at all it falls back to the dev server. Same port (8082), host and security headers either
# way (vite.config.ts sets both `server` and `preview`).
set -u
cd /mnt/d/Workspace/omni-tools || exit 1

stamp="$(git rev-parse HEAD 2>/dev/null) $(git -c core.filemode=false diff HEAD 2>/dev/null | md5sum | cut -d' ' -f1)"
if [ ! -f dist/index.html ] || [ "$(cat dist/.sol-build-stamp 2>/dev/null)" != "$stamp" ]; then
    echo "[omni-tools] code changed since the last build: building (about 1.5 min)"
    if npm run build; then
        echo "$stamp" > dist/.sol-build-stamp
    elif [ -f dist/index.html ]; then
        echo "[omni-tools] build failed: serving the previous build"
    else
        echo "[omni-tools] build failed and there is no previous build: starting the dev server"
        exec npm run dev
    fi
else
    echo "[omni-tools] build is current: serving it"
fi
exec npx vite preview
