#!/bin/bash
set -e

# 以非 root 身份執行時，不需要執行 chown
# 容器已在 Dockerfile 中設定 USER appuser
# 直接執行主程式
exec "$@"
