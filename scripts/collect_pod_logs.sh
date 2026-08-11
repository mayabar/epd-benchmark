#!/bin/bash

# Script to collect logs from all pods in a Kubernetes namespace for a specified time period
# Usage: ./collect_pod_logs.sh <namespace> [duration_in_minutes]
# Example: ./collect_pod_logs.sh default 30
# Example: ./collect_pod_logs.sh my-namespace 60

set -e

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Function to print colored messages
print_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

print_section() {
    echo -e "${BLUE}[====]${NC} $1"
}

# Check if namespace is provided
if [ -z "$1" ]; then
    print_error "Namespace not provided!"
    echo "Usage: $0 <namespace> [duration_in_minutes]"
    echo "Example: $0 default 30"
    echo "Example: $0 my-namespace 60"
    exit 1
fi

NAMESPACE=$1
DURATION_MINUTES=${2:-30}  # Default to 30 minutes if not specified

# Check if kubectl is installed
if ! command -v kubectl &> /dev/null; then
    print_error "kubectl is not installed or not in PATH"
    exit 1
fi

# Check if namespace exists
if ! kubectl get namespace "$NAMESPACE" &> /dev/null; then
    print_error "Namespace '$NAMESPACE' does not exist"
    exit 1
fi

# Create output directory with timestamp
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTPUT_DIR="pod_logs_${NAMESPACE}_${TIMESTAMP}"
mkdir -p "$OUTPUT_DIR"

print_info "Collecting logs from namespace: $NAMESPACE"
print_info "Time period: Last $DURATION_MINUTES minutes"
print_info "Output directory: $OUTPUT_DIR"
echo ""

# Calculate the since time
SINCE_TIME="${DURATION_MINUTES}m"

# Get all pods in the namespace
PODS=$(kubectl get pods -n "$NAMESPACE" -o jsonpath='{.items[*].metadata.name}')

if [ -z "$PODS" ]; then
    print_warning "No pods found in namespace '$NAMESPACE'"
    rmdir "$OUTPUT_DIR"
    exit 0
fi

POD_COUNT=$(echo "$PODS" | wc -w | tr -d ' ')
print_info "Found $POD_COUNT pod(s) in namespace '$NAMESPACE'"
echo ""

# Counter for progress
CURRENT=0

# Collect logs from each pod
for POD in $PODS; do
    CURRENT=$((CURRENT + 1))
    print_section "[$CURRENT/$POD_COUNT] Processing pod: $POD"
    
    # Get pod status
    POD_STATUS=$(kubectl get pod "$POD" -n "$NAMESPACE" -o jsonpath='{.status.phase}')
    print_info "Pod status: $POD_STATUS"
    
    # Get all container names in the pod (regular + init + ephemeral)
    CONTAINERS=$(kubectl get pod "$POD" -n "$NAMESPACE" -o jsonpath='{.spec.containers[*].name}')
    INIT_CONTAINERS=$(kubectl get pod "$POD" -n "$NAMESPACE" -o jsonpath='{.spec.initContainers[*].name}' 2>/dev/null || true)
    EPHEMERAL_CONTAINERS=$(kubectl get pod "$POD" -n "$NAMESPACE" -o jsonpath='{.spec.ephemeralContainers[*].name}' 2>/dev/null || true)
    ALL_CONTAINERS="$CONTAINERS $INIT_CONTAINERS $EPHEMERAL_CONTAINERS"

    if [ -z "$(echo "$ALL_CONTAINERS" | tr -d ' ')" ]; then
        print_warning "No containers found in pod '$POD'"
        continue
    fi

    # Create pod directory
    POD_DIR="$OUTPUT_DIR/$POD"
    mkdir -p "$POD_DIR"

    # Collect logs from each container
    for CONTAINER in $ALL_CONTAINERS; do
        [ -z "$CONTAINER" ] && continue
        print_info "  Collecting logs from container: $CONTAINER"
        
        LOG_FILE="$POD_DIR/${CONTAINER}.log"
        
        # Try to get logs with --since flag
        if kubectl logs "$POD" -n "$NAMESPACE" -c "$CONTAINER" --since="$SINCE_TIME" > "$LOG_FILE" 2>/dev/null; then
            LOG_SIZE=$(wc -l < "$LOG_FILE" | tr -d ' ')
            print_info "    ✓ Collected $LOG_SIZE lines"
        else
            # If --since fails, try without it (for very new pods)
            if kubectl logs "$POD" -n "$NAMESPACE" -c "$CONTAINER" > "$LOG_FILE" 2>/dev/null; then
                LOG_SIZE=$(wc -l < "$LOG_FILE" | tr -d ' ')
                print_warning "    ⚠ Could not use --since flag, collected all available logs ($LOG_SIZE lines)"
            else
                print_warning "    ✗ Failed to collect logs (container may not be ready)"
                rm -f "$LOG_FILE"
            fi
        fi
        
        # Try to get previous container logs if available (for crashed containers)
        PREV_LOG_FILE="$POD_DIR/${CONTAINER}_previous.log"
        if kubectl logs "$POD" -n "$NAMESPACE" -c "$CONTAINER" --previous --since="$SINCE_TIME" > "$PREV_LOG_FILE" 2>/dev/null; then
            PREV_LOG_SIZE=$(wc -l < "$PREV_LOG_FILE" | tr -d ' ')
            print_info "    ✓ Collected $PREV_LOG_SIZE lines from previous container"
        else
            rm -f "$PREV_LOG_FILE"
        fi
    done
    
    # Save pod description
    print_info "  Saving pod description"
    kubectl describe pod "$POD" -n "$NAMESPACE" > "$POD_DIR/pod_description.txt" 2>/dev/null || \
        print_warning "    ✗ Failed to get pod description"
    
    # Save pod YAML
    print_info "  Saving pod YAML"
    kubectl get pod "$POD" -n "$NAMESPACE" -o yaml > "$POD_DIR/pod.yaml" 2>/dev/null || \
        print_warning "    ✗ Failed to get pod YAML"
    
    echo ""
done

# Collect ConfigMaps with name suffix "-epp"
print_section "Collecting ConfigMaps with suffix '-epp'"
EPP_CONFIGMAPS=$(kubectl get configmaps -n "$NAMESPACE" -o jsonpath='{.items[*].metadata.name}' | tr ' ' '\n' | grep -- '-epp$' || true)

if [ -z "$EPP_CONFIGMAPS" ]; then
    print_warning "No ConfigMaps with suffix '-epp' found in namespace '$NAMESPACE'"
else
    CM_DIR="$OUTPUT_DIR/epp-configs"
    mkdir -p "$CM_DIR"
    for CM in $EPP_CONFIGMAPS; do
        print_info "  Collecting ConfigMap: $CM"
        kubectl get configmap "$CM" -n "$NAMESPACE" -o yaml > "$CM_DIR/${CM}.yaml" 2>/dev/null || \
            print_warning "    ✗ Failed to get ConfigMap '$CM'"
    done
fi
echo ""

# Create summary file
SUMMARY_FILE="$OUTPUT_DIR/collection_summary.txt"
{
    echo "Log Collection Summary"
    echo "======================"
    echo "Namespace: $NAMESPACE"
    echo "Time Period: Last $DURATION_MINUTES minutes"
    echo "Collection Time: $(date)"
    echo "Total Pods: $POD_COUNT"
    echo ""
    echo "Collected Logs:"
    echo "---------------"
    find "$OUTPUT_DIR" -name "*.log" -type f | while read -r logfile; do
        lines=$(wc -l < "$logfile" | tr -d ' ')
        size=$(du -h "$logfile" | cut -f1)
        echo "  $(basename "$(dirname "$logfile")")/$(basename "$logfile"): $lines lines, $size"
    done
} > "$SUMMARY_FILE"

print_section "Collection Complete!"
print_info "Logs saved to: $OUTPUT_DIR"
print_info "Summary saved to: $SUMMARY_FILE"
echo ""

# Display summary
cat "$SUMMARY_FILE"

# Create a tarball of the logs
TARBALL="${OUTPUT_DIR}.tar.gz"
print_info "Creating tarball: $TARBALL"
tar -czf "$TARBALL" "$OUTPUT_DIR" 2>/dev/null

if [ -f "$TARBALL" ]; then
    TARBALL_SIZE=$(du -h "$TARBALL" | cut -f1)
    print_info "✓ Tarball created successfully: $TARBALL ($TARBALL_SIZE)"
    echo ""
    print_info "To extract: tar -xzf $TARBALL"
else
    print_warning "Failed to create tarball"
fi

# Made with Bob
