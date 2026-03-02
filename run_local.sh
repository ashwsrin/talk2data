#!/bin/bash

# Helper script to run with Rancher Desktop (using nerdctl) or Docker
# Usage: ./run_local.sh [build]

# Function to check if a command exists
command_exists() {
  command -v "$1" >/dev/null 2>&1
}

# 1. Try standard Docker (Desktop or Rancher Desktop via docker-shim)
if command_exists docker; then
    echo "Docker detected."
    # Check if docker daemon is actually running
    if docker info >/dev/null 2>&1; then
        echo "Docker daemon is running."
        if [ "$1" == "build" ]; then
            docker compose build
        fi
        docker compose up
        exit 0
    else
        echo "Docker detected but daemon seems not running (docker info failed)."
    fi
fi

# 2. Try nerdctl (Rancher Desktop native)
if command_exists nerdctl; then
    echo "nerdctl detected."
    
    # Try default nerdctl
    if nerdctl info >/dev/null 2>&1; then
        echo "Using nerdctl (default socket)."
        CMD="nerdctl compose"
    # Try Rancher Desktop macOS socket
    elif [ -S "$HOME/.rd/docker.sock" ]; then
        echo "Found Rancher Desktop socket at ~/.rd/docker.sock"
        export CONTAINERD_ADDRESS="unix://$HOME/.rd/docker.sock"
        # Verify connection
        if nerdctl info >/dev/null 2>&1; then
             echo "Connected to Rancher Desktop via socket."
             CMD="nerdctl compose"
        else
             echo "Could not connect to Rancher Desktop socket (connection refused/permission denied)."
        fi
    fi

    if [ -n "$CMD" ]; then
        if [ "$1" == "build" ]; then
            $CMD build
        fi
        $CMD up
        exit 0
    fi
fi

echo "Error: No running container runtime found."
echo "Please ensure Docker Desktop or Rancher Desktop is running."
exit 1
