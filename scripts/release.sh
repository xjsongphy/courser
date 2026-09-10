#!/usr/bin/env bash
# 构建并发布 courser 到 PyPI。
#
# 依赖：需先 `uv build` 用到的 hatchling（uv 自动拉取）、发布用 twine（经 uvx）。
#
# 用法：
#   scripts/release.sh                 # 构建 sdist+wheel 并发布到正式 PyPI
#   scripts/release.sh --test          # 构建并发布到 Test PyPI（练手）
#
# 上传凭证（正式/Test 任选其一）：
#   - 环境变量：TWINE_USERNAME / TWINE_PASSWORD（或 TWINE_TOKEN）
#   - 或本地 ~/.pypirc 配置了 index-servers 的 pypi / testpypi
set -euo pipefail
cd "$(dirname "$0")/.."

TARGET="pypi"
if [[ "${1:-}" == "--test" ]]; then
    TARGET="testpypi"
fi

echo "==== 构建 sdist + wheel ===="
rm -rf dist
uv build

echo
echo "==== 本轮构建产物 ===="
ls -1 dist/

if [[ "$TARGET" == "testpypi" ]]; then
    echo
    echo "==== 发布到 Test PyPI ===="
    uvx twine upload --repository testpypi dist/*
else
    echo
    echo "==== 发布到正式 PyPI ===="
    uvx twine upload dist/*
fi

echo
VER=$(ls dist/*.whl | sed -E 's/.*-([0-9]+\.[0-9]+\.[0-9]+(-[a-zA-Z0-9.]+)?)-py3.*/\1/;q')
echo "✔ 发布完成：https://pypi.org/project/courser/${VER}/"
echo "  记得打 tag：git tag v${VER} && git push origin v${VER}"