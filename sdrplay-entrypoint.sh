#!/bin/sh
# Entrypoint of the Dockerfile.sdrplay image: SDRplay's API library only works while its daemon
# runs, and the two talk through /dev/shm, so the daemon lives in the same container.
# The container's init (compose `init: true`) reaps it; it dies with the container.
/opt/sdrplay_api/sdrplay_apiService &
# Give it a moment to find the RSP before trunk-recorder asks for it.
sleep 2
exec "$@"
