#!/usr/bin/env bash
# 全量/增量更新 A 股 qlib 日线数据（chenditc/investment_data 每日更新）
set -e
cd "$(dirname "$0")/.."
TARGET=data/qlib_home/.qlib/qlib_data/cn_data
echo "下载最新 qlib_bin.tar.gz ..."
URL="https://github.com/chenditc/investment_data/releases/latest/download/qlib_bin.tar.gz"
# 国内网络走 gh 代理，失败回退直连
curl -sL --retry 3 -o /tmp/qlib_bin.tar.gz "https://gh-proxy.com/${URL}" \
  || curl -sL --retry 3 -o /tmp/qlib_bin.tar.gz "https://ghfast.top/${URL}" \
  || curl -sL --retry 3 -o /tmp/qlib_bin.tar.gz "${URL}"
echo "解压到 $TARGET ..."
tar -zxf /tmp/qlib_bin.tar.gz -C "$TARGET" --strip-components=1
rm -f /tmp/qlib_bin.tar.gz
echo "完成。数据范围："
head -1 "$TARGET/calendars/day.txt"; tail -1 "$TARGET/calendars/day.txt"
