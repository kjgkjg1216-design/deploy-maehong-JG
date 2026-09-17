#!/bin/bash
# 부팅 시 앱을 멈춰 thrashing 방지 (수동 배포 후 다시 켤 것)
systemctl stop maehong
systemctl disable maehong
# swap 안전망
if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
