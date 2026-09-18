#!/bin/sh
set -eu
for url in http://grafana:3000/api/health http://loki:3100/ready http://tempo:3200/ready http://mimir:8080/ready http://alloy:12345/-/ready; do
  wget -q -T 1 -O /dev/null "$url" || exit 1
done
