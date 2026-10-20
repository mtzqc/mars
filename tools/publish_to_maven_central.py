#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
发布 AAR 到 Maven Central（Sonatype Central Portal），纯标准库实现。

流程（全部本地完成，最后一次性上传 bundle）：
  1. 生成 <artifact>-<version>.aar
  2. 生成 -sources.jar（打包 java 源码）
  3. 生成 -javadoc.jar（javadoc，缺失时用占位文档，Central 只要求存在）
  4. 生成完整 POM（name/description/url/licenses/developers/scm 齐全）
  5. 对每个文件生成 .asc 签名（GPG）
  6. 计算 md5/sha1/sha256/sha512 校验文件
  7. 按 Maven 目录结构打成 zip bundle
  8. 上传到 Central Portal: POST /api/v1/publisher/upload

用法:
    python3 tools/publish_to_maven_central.py \
        --group io.github.mtzqc --version 1.2.6 \
        --artifact mars-xlog=build/android/mars-xlog-1.2.6.aar \
        --artifact mars-xlog-nostl=build/android/mars-xlog-1.2.6-nostl.aar \
        --gpg-key <KEY_ID> \
        --sonatype-user <TOKEN_USER> --sonatype-pass <TOKEN_PASS>

凭据也可用环境变量: SONATYPE_USER / SONATYPE_PASS
"""
import argparse
import base64
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.sax.saxutils as sx
import zipfile

PORTAL_API = "https://central.sonatype.com/api/v1/publisher"

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
  <description>mars xlog for Android, with Android 15 (API 35) 16KB memory page size support. All ELF PT_LOAD segments of libmarsxlog.so are aligned to 16KB (0x4000) so the library loads on 16KB-page devices.</description>
  <url>https://github.com/mtzqc/mars</url>

  <licenses>
    <license>
      <name>MIT License</name>
      <url>https://opensource.org/licenses/MIT</url>
      <distribution>repo</distribution>
    </license>
  </licenses>

  <developers>
    <developer>
      <id>mtzqc</id>
      <name>MTZQC</name>
      <email>chendaye136@gmail.com</email>
      <url>https://github.com/mtzqc</url>
    </developer>
  </developers>

  <scm>
    <connection>scm:git:git://github.com/mtzqc/mars.git</connection>
    <developerConnection>scm:git:ssh://git@github.com/mtzqc/mars.git</developerConnection>
    <url>https://github.com/mtzqc/mars</url>
    <tag>HEAD</tag>
  </scm>
</project>
"""

JAVADOC_FALLBACK = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{artifact} {version}</title></head>
<body><h1>{artifact} {version}</h1>
<p>API documentation is not bundled in this artifact.</p>
<p>See <a href="https://github.com/mtzqc/mars">https://github.com/mtzqc/mars</a>
and <a href="https://github.com/mtzqc/mars/blob/master/ANDROID_16KB_PAGE.md">ANDROID_16KB_PAGE.md</a>.</p>
</body></html>
"""

CHUNK = 1 << 20


def run(cmd, **kw):
    """执行命令，失败时抛出带 stderr 的异常。"""
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
    if proc.returncode != 0:
        raise RuntimeError(
            "命令失败 (%d): %s\n%s" % (
                proc.returncode, " ".join(cmd),
                proc.stderr.decode("utf-8", "ignore")[:2000]))
    return proc.stdout


def digest(path, algo):
    h = hashlib.new(algo)
    with open(path, "rb") as fh:
        for blk in iter(lambda: fh.read(CHUNK), b""):
            h.update(blk)
    return h.hexdigest()


def write_checksums(path):
    """为单个文件写 md5/sha1/sha256/sha512 校验文件。"""
    for algo in ("md5", "sha1", "sha256", "sha512"):
        val = digest(path, algo)
        with open(path + "." + algo, "w", encoding="utf-8") as fh:
            fh.write(val)


def gpg_sign(path, key_id, gpg_homedir=None, gpg_exe=None):
    """生成 ASCII-armored 分离签名 <path>.asc。"""
    exe = gpg_exe or os.environ.get("GPG_EXE") or "gpg"
    cmd = [exe, "--batch", "--yes", "--armor", "--detach-sign",
           "-u", key_id, "-o", path + ".asc", path]
    homedir = gpg_homedir or os.environ.get("GPG_HOMEDIR")
    if homedir:
        cmd[1:1] = ["--homedir", homedir]
    run(cmd)


def make_sources_jar(java_src_dirs, out_path, artifact):
    """把 java 源码打进 sources jar。"""
    count = 0
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for src_dir in java_src_dirs:
            if not os.path.isdir(src_dir):
                continue
            for root, _dirs, files in os.walk(src_dir):
                for fn in files:
                    if not fn.endswith(".java"):
                        continue
                    full = os.path.join(root, fn)
                    arc = os.path.relpath(full, src_dir).replace(os.sep, "/")
                    zf.write(full, arc)
                    count += 1
        if count == 0:
            zf.writestr("README.txt", "No Java sources bundled for %s.\n" % artifact)
    return count


def make_javadoc_jar(java_src_dirs, out_path, artifact, version, android_jar, javadoc_exe):
    """尝试生成真实 javadoc，失败则用占位 HTML（Central 仅要求文件存在）。"""
    tmp = tempfile.mkdtemp(prefix="javadoc_")
    ok = False
    srcs = []
    for src_dir in java_src_dirs:
        if not os.path.isdir(src_dir):
            continue
        for root, _dirs, files in os.walk(src_dir):
            for fn in files:
                if fn.endswith(".java"):
                    srcs.append(os.path.join(root, fn))
    if srcs and javadoc_exe:
        cmd = [javadoc_exe, "-d", tmp, "-quiet", "-Xdoclint:none",
               "-encoding", "UTF-8", "-notimestamp"]
        if android_jar and os.path.exists(android_jar):
            cmd += ["-classpath", android_jar]
        cmd += srcs
        try:
            run(cmd)
            ok = True
        except Exception as exc:  # noqa: BLE001
            print("  [warn] javadoc 生成失败，使用占位文档: %s" % str(exc).split("\n")[0])
    if not ok:
        with open(os.path.join(tmp, "index.html"), "w", encoding="utf-8") as fh:
            fh.write(JAVADOC_FALLBACK.format(artifact=artifact, version=version))

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(tmp):
            for fn in files:
                full = os.path.join(root, fn)
                arc = os.path.relpath(full, tmp).replace(os.sep, "/")
                zf.write(full, arc)
    shutil.rmtree(tmp, ignore_errors=True)
    return ok


def build_bundle(stage_root, out_zip):
    """把 stage 目录按原结构压成 zip（Central 要求的 bundle 格式）。"""
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(stage_root):
            for fn in files:
                full = os.path.join(root, fn)
                arc = os.path.relpath(full, stage_root).replace(os.sep, "/")
                zf.write(full, arc)
    return out_zip


def upload_bundle(zip_path, user, password, name, publishing_type="AUTOMATIC"):
    """上传 bundle 到 Central Portal，返回 deployment id。"""
    url = "%s/upload?name=%s&publishingType=%s" % (
        PORTAL_API, urllib.parse.quote(name), publishing_type)
    boundary = "----MarsBoundary7x" + os.urandom(8).hex()
    with open(zip_path, "rb") as fh:
        payload = fh.read()

    body = io.BytesIO()
    body.write(("--%s\r\n" % boundary).encode())
    body.write(b'Content-Disposition: form-data; name="bundle"; '
               b'filename="bundle.zip"\r\n')
    body.write(b"Content-Type: application/octet-stream\r\n\r\n")
    body.write(payload)
    body.write(("\r\n--%s--\r\n" % boundary).encode())
    data = body.getvalue()

    req = urllib.request.Request(url, data=data, method="POST")
    cred = base64.b64encode(("%s:%s" % (user, password)).encode()).decode()
    req.add_header("Authorization", "Basic " + cred)
    req.add_header("Content-Type", "multipart/form-data; boundary=%s" % boundary)
    req.add_header("Content-Length", str(len(data)))
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return resp.status, resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "ignore")[:2000]
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", default="io.github.mtzqc")
    ap.add_argument("--version", required=True)
    ap.add_argument("--artifact", action="append", default=[],
                    metavar="artifactId=aarPath", help="可重复")
    ap.add_argument("--java-src", action="append", default=[],
                    help="java 源码根目录，可重复")
    ap.add_argument("--gpg-key", required=True, help="签名用 key id")
    ap.add_argument("--gpg-homedir", default="")
    ap.add_argument("--gpg-exe", default="", help="gpg 可执行文件路径，默认取 GPG_EXE 或 gpg")
    ap.add_argument("--android-jar", default="")
    ap.add_argument("--javadoc-exe", default="javadoc")
    ap.add_argument("--sonatype-user", default=os.environ.get("SONATYPE_USER", ""))
    ap.add_argument("--sonatype-pass", default=os.environ.get("SONATYPE_PASS", ""))
    ap.add_argument("--stage-dir", default="build/maven-central")
    ap.add_argument("--publishing-type", default="AUTOMATIC",
                    choices=["AUTOMATIC", "USER_MANAGED"])
    ap.add_argument("--skip-upload", action="store_true",
                    help="只生成 bundle 不上传")
    args = ap.parse_args()

    group_path = args.group.replace(".", "/")
    stage_root = os.path.abspath(args.stage_dir)
    if os.path.isdir(stage_root):
        shutil.rmtree(stage_root)
    os.makedirs(stage_root, exist_ok=True)

    src_dirs = args.java_src or [
        "mars/libraries/mars_android_sdk/src/main/java",
    ]

    print("== 1/4 生成产物、POM、sources、javadoc ==")
    for item in args.artifact:
        if "=" not in item:
            print("[fail] --artifact 需要 artifactId=path: %s" % item, file=sys.stderr)
            return 1
        artifact, aar_path = item.split("=", 1)
        if not os.path.exists(aar_path):
            print("[fail] AAR 不存在: %s" % aar_path, file=sys.stderr)
            return 1

        ver_dir = os.path.join(stage_root, group_path, artifact, args.version)
        os.makedirs(ver_dir, exist_ok=True)
        base = "%s-%s" % (artifact, args.version)

        # AAR
        shutil.copy2(aar_path, os.path.join(ver_dir, base + ".aar"))

        # POM
        pom = POM_TEMPLATE.format(group=args.group, artifact=artifact,
                                  version=args.version)
        with open(os.path.join(ver_dir, base + ".pom"), "w", encoding="utf-8") as fh:
            fh.write(pom)

        # sources
        src_jar = os.path.join(ver_dir, base + "-sources.jar")
        n = make_sources_jar(src_dirs, src_jar, artifact)

        # javadoc
        doc_jar = os.path.join(ver_dir, base + "-javadoc.jar")
        real = make_javadoc_jar(src_dirs, doc_jar, artifact, args.version,
                                args.android_jar, args.javadoc_exe)
        print("  %s: aar + pom + sources(%d files) + javadoc(%s)"
              % (artifact, n, "real" if real else "fallback"))

    print("== 2/4 GPG 签名 ==")
    signed = 0
    for root, _dirs, files in os.walk(stage_root):
        for fn in files:
            if fn.endswith(".asc"):
                continue
            full = os.path.join(root, fn)
            gpg_sign(full, args.gpg_key, args.gpg_homedir or None,
                     args.gpg_exe or None)
            signed += 1
    print("  已签名 %d 个文件" % signed)

    print("== 3/4 生成校验文件 ==")
    n_ck = 0
    for root, _dirs, files in os.walk(stage_root):
        for fn in files:
            if re.search(r"\.(md5|sha1|sha256|sha512|asc)$", fn):
                continue
            write_checksums(os.path.join(root, fn))
            n_ck += 1
    print("  已为 %d 个文件生成 md5/sha1/sha256/sha512" % n_ck)

    print("== 4/4 打包 bundle ==")
    bundle = build_bundle(stage_root, os.path.join(stage_root, "..",
                                                   "maven-central-bundle.zip"))
    bundle = os.path.abspath(bundle)
    size_mb = os.path.getsize(bundle) / 1024.0 / 1024.0
    print("  bundle: %s (%.2f MB)" % (bundle, size_mb))

    print("\n坐标:")
    for item in args.artifact:
        print("  implementation '%s:%s:%s'"
              % (args.group, item.split("=", 1)[0], args.version))

    if args.skip_upload:
        print("\n[skip-upload] 未上传。bundle 已就绪。")
        return 0

    if not args.sonatype_user or not args.sonatype_pass:
        print("\n[fail] 缺少 Sonatype 凭据（--sonatype-user/--sonatype-pass "
              "或 SONATYPE_USER/SONATYPE_PASS）", file=sys.stderr)
        return 1

    print("\n== 上传到 Central Portal ==")
    status, body = upload_bundle(bundle, args.sonatype_user, args.sonatype_pass,
                                 "mars-xlog-%s" % args.version,
                                 args.publishing_type)
    if status in (200, 201, 202):
        print("  [ok] HTTP %s" % status)
        print("  deployment: %s" % body.strip())
        print("\n后续: 到 https://central.sonatype.com/publishing/deployments 查看进度")
        return 0
    print("  [fail] HTTP %s\n%s" % (status, body), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
