#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
发布 AAR 到 GitHub Packages（Maven registry），纯标准库实现，不依赖 mvn/gradle。

用法:
    python3 tools/publish_to_github_packages.py \
        --owner mtzqc --repo mars --version 1.2.6 \
        --artifact mars-xlog=build/android/mars-xlog-1.2.6.aar \
        --artifact mars-xlog-nostl=build/android/mars-xlog-1.2.6-nostl.aar

凭据（二选一，优先级从高到低）:
    --token / 环境变量 GH_TOKEN / 环境变量 GITHUB_TOKEN
    都不传时自动执行 `gh auth token` 获取（需该 token 带 write:packages）

说明:
    - groupId 默认 cn.mtzqc.mars，避免与官方 com.tencent.mars:mars-xlog 坐标冲突
    - 可用 --group 覆盖
    - 上传坐标: <group>/<artifact>/<version>/<artifact>-<version>.aar  + .pom
    - 已存在（409）视为成功，方便重复执行
"""
import argparse
import base64
import os
import subprocess
import sys
import urllib.error
import urllib.request

GROUP_ID = "cn.mtzqc.mars"
GROUP_PATH = GROUP_ID.replace(".", "/")
POM_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 http://maven.apache.org/xsd/maven-4.0.0.xsd">
  <modelVersion>4.0.0</modelVersion>
  <groupId>{group}</groupId>
  <artifactId>{artifact}</artifactId>
  <version>{version}</version>
  <packaging>aar</packaging>
  <name>{artifact}</name>
  <description>mars xlog (Android 15 16KB page size ready){desc}</description>
  <url>https://github.com/Tencent/mars</url>
  <licenses>
    <license>
      <name>MIT License</name>
      <url>http://opensource.org/licenses/MIT</url>
    </license>
  </licenses>
</project>
"""


def get_token(args):
    if args.token:
        return args.token
    for env in ("GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(env):
            return os.environ[env]
    try:
        out = subprocess.check_output(["gh", "auth", "token"], stderr=subprocess.DEVNULL)
        return out.decode().strip()
    except Exception:  # noqa: BLE001
        return ""


def put(url, data, token, user, content_type):
    req = urllib.request.Request(url, data=data, method="PUT")
    raw = ("%s:%s" % (user, token)).encode()
    req.add_header("Authorization", "Basic " + base64.b64encode(raw).decode())
    req.add_header("Content-Type", content_type)
    req.add_header("Content-Length", str(len(data)))
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, ""
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "ignore")[:300]
        return exc.code, body
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--owner", required=True, help="GitHub 用户名/组织")
    ap.add_argument("--repo", required=True, help="仓库名，Packages 挂在它下面")
    ap.add_argument("--version", required=True)
    ap.add_argument("--group", default=GROUP_ID,
                    help="groupId，默认 %s" % GROUP_ID)
    ap.add_argument("--artifact", action="append", default=[],
                    metavar="artifactId=path", help="可重复")
    ap.add_argument("--token", default="")
    ap.add_argument("--user", default="", help="默认等于 --owner")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    user = args.user or args.owner
    group_id = args.group
    group_path = group_id.replace(".", "/")
    token = get_token(args)
    if not token:
        print("[fail] 拿不到 token，请设置 GH_TOKEN 或先 gh auth login", file=sys.stderr)
        return 1

    base = "https://maven.pkg.github.com/%s/%s" % (args.owner, args.repo)
    ok = True
    for item in args.artifact:
        if "=" not in item:
            print("[fail] --artifact 需要 artifactId=path 形式: %s" % item, file=sys.stderr)
            return 1
        artifact, path = item.split("=", 1)
        if not os.path.exists(path):
            print("[fail] 文件不存在: %s" % path, file=sys.stderr)
            ok = False
            continue

        ver_dir = "%s/%s/%s/%s" % (base, group_path, artifact, args.version)
        fname = "%s-%s.aar" % (artifact, args.version)
        pom = POM_TEMPLATE.format(
            group=group_id, artifact=artifact, version=args.version,
            desc=" (bundled libc++_shared)" if artifact == "mars-xlog" else " (no libc++_shared)")

        jobs = [
            ("%s/%s" % (ver_dir, fname), open(path, "rb").read(), "application/octet-stream"),
            ("%s/%s-%s.pom" % (ver_dir, artifact, args.version),
             pom.encode("utf-8"), "application/xml"),
        ]
        for url, data, ctype in jobs:
            if args.dry_run:
                print("[dry-run] PUT %s (%d bytes)" % (url, len(data)))
                continue
            status, body = put(url, data, token, user, ctype)
            if status in (200, 201, 204):
                print("[ok]   %s" % url.rsplit("/", 1)[-1])
            elif status == 409:
                print("[skip] 已存在: %s" % url.rsplit("/", 1)[-1])
            else:
                ok = False
                print("[fail] %s -> HTTP %s %s" % (url, status, body), file=sys.stderr)

    print("\n依赖写法:")
    for item in args.artifact:
        artifact = item.split("=", 1)[0]
        print("  implementation '%s:%s:%s'" % (group_id, artifact, args.version))
    print("\n仓库配置:")
    print("  maven { url 'https://maven.pkg.github.com/%s/%s'; credentials { username = '%s'; password = <PAT> } }"
          % (args.owner, args.repo, user))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
