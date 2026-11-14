#!/bin/bash
# Helmraiser Git Helper Script
# Use this script to create branches, commit, and create PRs

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Usage function
usage() {
    echo -e "${BLUE}Helmraiser Git Helper${NC}"
    echo ""
    echo "Usage: $0 <command> [options]"
    echo ""
    echo "Commands:"
    echo "  branch <name>     Create and checkout a feature branch"
    echo "  commit <message>  Stage all changes and commit"
    echo "  push              Push current branch to origin"
    echo "  pr [title]        Create a PR for current branch"
    echo "  full <message>    Branch + Commit + Push + PR (all in one)"
    echo ""
    echo "Examples:"
    echo "  $0 branch feature/my-new-feature"
    echo "  $0 commit \"feat: Add new feature\""
    echo "  $0 push"
    echo "  $0 pr \"Add new feature\""
    echo "  $0 full \"feat: Complete feature\" feature/my-feature"
    exit 1
}

# Create branch
create_branch() {
    local branch_name=$1
    if [ -z "$branch_name" ]; then
        echo -e "${RED}Error: Branch name required${NC}"
        usage
    fi
    
    echo -e "${YELLOW}Creating branch: $branch_name${NC}"
    git checkout -b "$branch_name"
    echo -e "${GREEN}✓ Branch created: $branch_name${NC}"
}

# Commit changes
commit_changes() {
    local message=$1
    if [ -z "$message" ]; then
        echo -e "${RED}Error: Commit message required${NC}"
        usage
    fi
    
    echo -e "${YELLOW}Staging all changes...${NC}"
    git add -A
    
    echo -e "${YELLOW}Committing: $message${NC}"
    git commit -m "$message"
    echo -e "${GREEN}✓ Changes committed${NC}"
}

# Push branch
push_branch() {
    local current_branch=$(git rev-parse --abbrev-ref HEAD)
    
    echo -e "${YELLOW}Pushing branch: $current_branch${NC}"
    git push -u origin "$current_branch"
    echo -e "${GREEN}✓ Branch pushed to origin${NC}"
}

# Create PR
create_pr() {
    local title=$1
    local current_branch=$(git rev-parse --abbrev-ref HEAD)
    
    if [ -z "$title" ]; then
        title=$(git log -1 --pretty=%B | head -1)
    fi
    
    echo -e "${YELLOW}Creating PR for: $current_branch${NC}"
    echo -e "${YELLOW}Title: $title${NC}"
    
    # Check if gh CLI is available
    if ! command -v gh &> /dev/null; then
        echo -e "${RED}Error: gh CLI not found. Please install: https://cli.github.com/${NC}"
        exit 1
    fi
    
    # Create PR
    gh pr create --title "$title" --body "Automated PR from Helmraiser agent" --base main
    echo -e "${GREEN}✓ PR created${NC}"
}

# Full workflow
full_workflow() {
    local message=$1
    local branch_name=$2
    
    if [ -z "$message" ] || [ -z "$branch_name" ]; then
        echo -e "${RED}Error: Message and branch name required${NC}"
        usage
    fi
    
    create_branch "$branch_name"
    commit_changes "$message"
    push_branch
    create_pr "$message"
    
    echo -e "${GREEN}================================${NC}"
    echo -e "${GREEN}✓ Full workflow complete!${NC}"
    echo -e "${GREEN}================================${NC}"
}

# Main command handler
case "$1" in
    branch)
        create_branch "$2"
        ;;
    commit)
        commit_changes "$2"
        ;;
    push)
        push_branch
        ;;
    pr)
        create_pr "$2"
        ;;
    full)
        full_workflow "$2" "$3"
        ;;
    *)
        usage
        ;;
esac
