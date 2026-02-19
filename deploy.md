# Full deploy (upload code + fix env + build + start + health check)
bash deploy.sh

# Just push code changes, then rebuild
bash deploy.sh --code-only
bash deploy.sh --rebuild

# Quick restart (no rebuild)
bash deploy.sh --restart

# Seed test data
bash deploy.sh --seed

# Check everything is healthy
bash deploy.sh --status

# Watch live logs
bash deploy.sh --logs