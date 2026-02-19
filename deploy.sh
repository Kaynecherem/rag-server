#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
# deploy.sh — One-command deployment for Insurance RAG
#
# Usage (from PowerShell on Windows):
#   bash deploy.sh                  # Full deploy (upload + build + start)
#   bash deploy.sh --code-only      # Just upload code, no rebuild
#   bash deploy.sh --restart        # Restart containers without rebuild
#   bash deploy.sh --logs           # Tail API logs
#   bash deploy.sh --status         # Check container status + health
#
# Run from: C:\Users\kaluc\IdeaProjects\insurance-rag\
# Requires: SSH key at ~/.ssh/insurance-rag2.pem
# ═══════════════════════════════════════════════════════════════════════

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────
EC2_IP="18.211.76.143"
SSH_KEY="$HOME/.ssh/insurance-rag2.pem"
SSH_USER="ec2-user"
REMOTE_DIR="/opt/insurance-rag"
CLOUDFRONT_URL="https://d28pes0iok9s89.cloudfront.net"

SSH_CMD="ssh -i $SSH_KEY -o StrictHostKeyChecking=no -o ConnectTimeout=10 $SSH_USER@$EC2_IP"
SCP_CMD="scp -i $SSH_KEY -o StrictHostKeyChecking=no"

# ── Colors ────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[✓]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
error() { echo -e "${RED}[✗]${NC} $1"; }
step()  { echo -e "\n${GREEN}═══${NC} $1 ${GREEN}═══${NC}"; }

# ── Preflight Checks ─────────────────────────────────────────────────
preflight() {
    step "Preflight checks"

    if [ ! -f "$SSH_KEY" ]; then
        error "SSH key not found: $SSH_KEY"
        exit 1
    fi
    info "SSH key found"

    if [ ! -d "app" ]; then
        error "Run this script from the insurance-rag project root (app/ directory not found)"
        exit 1
    fi
    info "Project directory OK"

    # Test SSH connection
    if ! $SSH_CMD "echo ok" &>/dev/null; then
        error "Cannot SSH to $EC2_IP — your IP may have changed."
        echo "  1. Check your IP:  curl ifconfig.me"
        echo "  2. Update my_ip in terraform.tfvars"
        echo "  3. Run: terraform apply"
        exit 1
    fi
    info "SSH connection OK"
}

# ── Bootstrap: ensure Docker buildx + ownership ──────────────────────
bootstrap() {
    step "Bootstrapping server"

    $SSH_CMD << 'REMOTE_SCRIPT'
        set -e

        # Fix ownership
        sudo chown -R ec2-user:ec2-user /opt/insurance-rag

        # Install buildx if missing
        if ! docker buildx version &>/dev/null; then
            echo "[!] Installing Docker Buildx..."
            sudo mkdir -p /usr/local/lib/docker/cli-plugins
            sudo curl -sSL "https://github.com/docker/buildx/releases/download/v0.19.3/buildx-v0.19.3.linux-amd64" \
                -o /usr/local/lib/docker/cli-plugins/docker-buildx
            sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-buildx
            echo "[✓] Buildx installed: $(docker buildx version)"
        else
            echo "[✓] Buildx already installed"
        fi

        # Ensure docker is running
        if ! sudo systemctl is-active docker &>/dev/null; then
            sudo systemctl start docker
            echo "[✓] Docker started"
        else
            echo "[✓] Docker running"
        fi
REMOTE_SCRIPT

    info "Bootstrap complete"
}

# ── Upload Code ───────────────────────────────────────────────────────
upload_code() {
    step "Uploading application code"

    # Upload app directory and key files
    $SCP_CMD -r app "$SSH_USER@$EC2_IP:$REMOTE_DIR/"
    info "app/ uploaded"

    $SCP_CMD requirements.txt "$SSH_USER@$EC2_IP:$REMOTE_DIR/"
    info "requirements.txt uploaded"

    # Upload Dockerfile.prod if it exists locally
    if [ -f "Dockerfile.prod" ]; then
        $SCP_CMD Dockerfile.prod "$SSH_USER@$EC2_IP:$REMOTE_DIR/"
        info "Dockerfile.prod uploaded"
    fi

    info "Code upload complete"
}

# ── Fix Environment ──────────────────────────────────────────────────
fix_env() {
    step "Fixing environment"

    $SSH_CMD << 'REMOTE_SCRIPT'
        set -e
        cd /opt/insurance-rag

        # Remove POSTGRES_ vars from .env (they cause pydantic errors)
        if grep -q "^POSTGRES_" .env 2>/dev/null; then
            sed -i '/^POSTGRES_/d' .env
            echo "[✓] Removed POSTGRES_ vars from .env"
        else
            echo "[✓] .env already clean"
        fi

        # Ensure DEBUG=true for demo
        if grep -q "^DEBUG=false" .env 2>/dev/null; then
            sed -i 's/^DEBUG=false/DEBUG=true/' .env
            echo "[✓] Set DEBUG=true"
        fi
REMOTE_SCRIPT

    info "Environment fixed"
}

# ── Write docker-compose.yml ─────────────────────────────────────────
write_compose() {
    step "Writing docker-compose.yml"

    $SSH_CMD << 'REMOTE_SCRIPT'
cat > /opt/insurance-rag/docker-compose.yml << 'EOF'
services:
  db:
    image: postgres:15-alpine
    restart: always
    environment:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres
      POSTGRES_DB: insurance_rag
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 10s
      timeout: 5s
      retries: 5
  redis:
    image: redis:7-alpine
    restart: always
    command: redis-server --maxmemory 64mb --maxmemory-policy allkeys-lru
    volumes:
      - redisdata:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5
  api:
    build:
      context: .
      dockerfile: Dockerfile.prod
    restart: always
    env_file: .env
    ports:
      - "80:8000"
    volumes:
      - localstorage:/app/storage
    depends_on:
      db:
        condition: service_healthy
      redis:
        condition: service_healthy
  watchtower:
    image: containrrr/watchtower
    restart: always
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    command: --interval 300 --cleanup
    environment:
      - WATCHTOWER_LABEL_ENABLE=false
volumes:
  pgdata:
  redisdata:
  localstorage:
EOF
        echo "[✓] docker-compose.yml written"
REMOTE_SCRIPT

    info "docker-compose.yml ready"
}

# ── Build & Start ────────────────────────────────────────────────────
build_and_start() {
    step "Building and starting containers"

    $SSH_CMD << 'REMOTE_SCRIPT'
        set -e
        cd /opt/insurance-rag
        sudo docker compose down 2>/dev/null || true
        sudo docker compose up -d --build
        echo ""
        echo "Waiting for API to start..."
        sleep 8

        # Health check
        for i in 1 2 3 4 5; do
            if curl -sf http://localhost/health > /dev/null 2>&1; then
                echo "[✓] API is healthy!"
                curl -s http://localhost/health | python3 -m json.tool 2>/dev/null || curl -s http://localhost/health
                exit 0
            fi
            echo "  Attempt $i/5 - waiting..."
            sleep 5
        done

        echo "[✗] API did not start. Checking logs..."
        sudo docker compose logs api --tail 20
        exit 1
REMOTE_SCRIPT

    info "Containers running"
}

# ── Restart Only ─────────────────────────────────────────────────────
restart_only() {
    step "Restarting containers"

    $SSH_CMD << 'REMOTE_SCRIPT'
        set -e
        cd /opt/insurance-rag
        sudo docker compose down
        sudo docker compose up -d
        sleep 8
        curl -sf http://localhost/health && echo " [✓] Healthy" || echo " [✗] Not healthy"
REMOTE_SCRIPT
}

# ── Status ───────────────────────────────────────────────────────────
show_status() {
    step "System Status"

    $SSH_CMD << REMOTE_SCRIPT
        echo "── Containers ──"
        sudo docker compose -f $REMOTE_DIR/docker-compose.yml ps 2>/dev/null || echo "No containers"
        echo ""
        echo "── Health Check (local) ──"
        curl -sf http://localhost/health 2>/dev/null && echo "" || echo "API not responding"
        echo ""
        echo "── Disk ──"
        df -h / | tail -1
        echo ""
        echo "── Memory ──"
        free -m | head -2
        echo ""
        echo "── CloudFront ──"
        curl -sf $CLOUDFRONT_URL/health 2>/dev/null && echo "" || echo "CloudFront not responding"
REMOTE_SCRIPT
}

# ── Logs ─────────────────────────────────────────────────────────────
show_logs() {
    step "API Logs (Ctrl+C to stop)"
    $SSH_CMD "cd $REMOTE_DIR && sudo docker compose logs api --tail 50 -f"
}

# ── Seed Test Data ───────────────────────────────────────────────────
seed_data() {
    step "Seeding test data"
    $SSH_CMD "curl -sf -X POST http://localhost/api/v1/auth/test-setup | python3 -m json.tool 2>/dev/null || curl -s -X POST http://localhost/api/v1/auth/test-setup"
    info "Test data seeded"
}

# ── Main ─────────────────────────────────────────────────────────────
case "${1:-deploy}" in
    --code-only)
        preflight
        upload_code
        info "Code uploaded. Run 'bash deploy.sh --restart' to apply."
        ;;
    --restart)
        preflight
        fix_env
        restart_only
        ;;
    --rebuild)
        preflight
        fix_env
        build_and_start
        ;;
    --logs)
        show_logs
        ;;
    --status)
        show_status
        ;;
    --seed)
        seed_data
        ;;
    deploy|"")
        preflight
        bootstrap
        upload_code
        fix_env
        write_compose
        build_and_start
        echo ""
        step "Deployment Complete"
        info "API:        $CLOUDFRONT_URL"
        info "Health:     $CLOUDFRONT_URL/health"
        info "SSH:        ssh -i $SSH_KEY $SSH_USER@$EC2_IP"
        info ""
        info "Next: bash deploy.sh --seed    (seed test data)"
        info "      bash deploy.sh --logs    (watch logs)"
        info "      bash deploy.sh --status  (check health)"
        ;;
    *)
        echo "Usage: bash deploy.sh [command]"
        echo ""
        echo "Commands:"
        echo "  (default)     Full deploy: upload + build + start"
        echo "  --code-only   Upload code without rebuilding"
        echo "  --restart     Restart containers (no rebuild)"
        echo "  --rebuild     Rebuild and restart containers"
        echo "  --logs        Tail API logs"
        echo "  --status      Show system status"
        echo "  --seed        Seed test data"
        ;;
esac