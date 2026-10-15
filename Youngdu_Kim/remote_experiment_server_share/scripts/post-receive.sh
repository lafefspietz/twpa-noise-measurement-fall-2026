#!/usr/bin/env bash
# Optional git post-receive hook for the server's copy of this repository, so that a
# `git push` to it deploys. One-time setup in that copy:
#   git config receive.denyCurrentBranch updateInstead
#   ln -sf ../../scripts/post-receive.sh .git/hooks/post-receive
# With updateInstead, a push to the checked-out branch has already updated the files when
# this runs. If server files changed, this restarts the server with the arguments it was
# running with (see restart-server.sh).
set -u
cd "$(git rev-parse --git-dir)/.." || exit 1
unset $(git rev-parse --local-env-vars)

branch=$(git symbolic-ref -q HEAD)
changed=""
while read -r old new ref; do
    [[ "$ref" == "$branch" ]] || continue
    if [[ "$old" =~ ^0+$ ]]; then
        changed=$(git ls-tree -r --name-only "$new")
    else
        changed=$(git diff --name-only "$old" "$new")
    fi
done

if [[ -z "$changed" ]]; then
    echo "checked-out branch ${branch#refs/heads/} not updated; not restarting"
    exit 0
fi
if ! grep -qE '^(labserver/|config\.yaml$|requirements\.txt$|run-server\.sh$)' <<< "$changed"; then
    echo "no server files changed; not restarting"
    exit 0
fi

if grep -qx 'requirements.txt' <<< "$changed"; then
    echo "requirements.txt changed: installing packages"
    # a venv made by uv has no pip; hooks don't get the login PATH, so look in ~/.local/bin too
    uv=$(command -v uv || echo "$HOME/.local/bin/uv")
    if [[ -x .venv/bin/pip ]]; then
        install=(.venv/bin/pip install -q -r requirements.txt)
    elif [[ -x "$uv" ]]; then
        install=("$uv" pip install -q --python .venv/bin/python -r requirements.txt)
    else
        echo "no pip or uv for .venv; install requirements.txt by hand"
        exit 1
    fi
    if ! "${install[@]}"; then
        echo "pip install failed; not restarting"
        exit 1
    fi
fi

scripts/restart-server.sh
