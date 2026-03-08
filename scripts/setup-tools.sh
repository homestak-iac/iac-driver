#!/bin/bash
# Setup or update tool repositories
# Usage: setup-tools.sh [options] [base_dir]
#
# Clones ansible, tofu, and packer repos if they don't exist,
# or pulls latest changes if they do.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GITHUB_ORG="homestak-iac"

show_help() {
    cat << 'EOF'
setup-tools.sh - Setup or update tool repositories

Usage:
  setup-tools.sh [options] [base_dir]

Options:
  --help, -h    Show this help message

Arguments:
  base_dir      Base directory for repos (default: parent of iac-driver)

Description:
  Clones ansible, tofu, and packer repos as siblings to iac-driver.
  If repos already exist, pulls latest changes instead.

Repositories:
  - ansible        Playbooks and roles
  - tofu           VM provisioning
  - packer         Cloud image building

Examples:
  ./setup-tools.sh                    # Use default base directory
  ./setup-tools.sh ~/lib               # Specify custom base directory
EOF
    exit 0
}

# Parse arguments
if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    show_help
fi

BASE_DIR="${1:-$(dirname "$(dirname "$SCRIPT_DIR")")}"

declare -A REPOS=(
  [ansible]="https://github.com/$GITHUB_ORG/ansible.git"
  [tofu]="https://github.com/$GITHUB_ORG/tofu.git"
  [packer]="https://github.com/$GITHUB_ORG/packer.git"

)

echo "Setting up tool repositories in: $BASE_DIR"

for repo in "${!REPOS[@]}"; do
  target="$BASE_DIR/$repo"
  if [[ -d "$target/.git" ]]; then
    echo "Updating $repo..."
    git -C "$target" pull --ff-only
  else
    echo "Cloning $repo..."
    git clone "${REPOS[$repo]}" "$target"
  fi
done

echo "Done. Tool repositories are ready."
