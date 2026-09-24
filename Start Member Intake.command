#!/bin/sh
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$project_dir" || exit 1
if [ ! -x "$project_dir/.venv/bin/python" ]; then
    echo 'Create this project’s .venv and install requirements.txt first (see README).'
    read -r reply
    exit 1
fi
"$project_dir/.venv/bin/python" "$project_dir/local_app.py"
result=$?
if [ "$result" -ne 0 ]; then
    echo 'Member Intake stopped. Press Enter to close this window.'
    read -r reply
fi
exit "$result"
