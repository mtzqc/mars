#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把编译好的 xlog 产物组装成 AAR（不依赖 Gradle/AGP）。

用法:
    python3 tools/make_xlog_aar.py \
        --mars-dir <repo> --work-dir <work> --out-dir <out> \
        --version 1.2.6 --abis "arm64-v8a armeabi-v7a" \
        [--variant full|nostl] [--strip <llvm-strip>]

variant:
    full   含 jni/<abi>/libc++_shared.so（默认），产物 mars-xlog-<ver>.aar
    nostl  不含 libc++_shared.so，需宿主 App 自行提供，产物 mars-xlog-<ver>-nostl.aar

strip:
    指定 llvm-strip 路径时打包前先 strip（未 strip 的副本另存到
    <out-dir>/symbols/<abi>/ 用于符号化），so 体积通常从 ~7MB 降到 ~500KB。

AAR 结构（AAR 规范）:
    AndroidManifest.xml
    classes.jar
    R.txt
    proguard.txt
    jni/<abi>/libmarsxlog.so
    jni/<abi>/libc++_shared.so    # 仅 variant=full
"""
import argparse
import os
import shutil
import subprocess
import sys
import zipfile

# ABI -> (work 下的构建目录名, NDK sysroot triple)
ABI_INFO = {
    "arm64-v8a": ("arm64-v8a", "aarch64-linux-android"),
    "armeabi-v7a": ("armeabi-v7a", "arm-linux-androideabi"),
    "x86_64": ("x86_64", "x86_64-linux-android"),
}


def find_sysroot_lib(ndk):
    base = os.path.join(ndk, "toolchains/llvm/prebuilt")
    if not os.path.isdir(base):
        return ""
    for host in os.listdir(base):
        cand = os.path.join(base, host, "sysroot/usr/lib")
        if os.path.isdir(cand):
            return cand
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mars-dir", required=True)
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--abis", required=True)
    ap.add_argument("--ndk", default=os.environ.get("ANDROID_NDK_HOME", ""))
    ap.add_argument("--variant", choices=("full", "nostl"), default="full")
    ap.add_argument("--strip", default="", help="llvm-strip 可执行文件路径，留空则不 strip")
    args = ap.parse_args()

    mars, work, out_dir = args.mars_dir, args.work_dir, args.out_dir
    abis = args.abis.split()
    sdk = os.path.join(mars, "mars/libraries/mars_xlog_sdk")

    stage = os.path.join(work, "aarstage-%s" % args.variant)
    if os.path.isdir(stage):
        shutil.rmtree(stage)
    os.makedirs(stage)

    # manifest / proguard / R.txt
    shutil.copy(os.path.join(sdk, "src/main/AndroidManifest.xml"),
                os.path.join(stage, "AndroidManifest.xml"))
    shutil.copy(os.path.join(sdk, "proguard-rules.pro"),
                os.path.join(stage, "proguard.txt"))
    open(os.path.join(stage, "R.txt"), "w").close()

    # classes.jar
    classes_jar = os.path.join(work, "classes.jar")
    if not os.path.exists(classes_jar):
        print("[fail] 缺少 classes.jar: %s" % classes_jar, file=sys.stderr)
        return 1
    shutil.copy(classes_jar, os.path.join(stage, "classes.jar"))

    sysroot_lib = find_sysroot_lib(args.ndk) if args.variant == "full" else ""

    for abi in abis:
        if abi not in ABI_INFO:
            print("[warn] 未知 ABI，跳过: %s" % abi, file=sys.stderr)
            continue
        build_dir, triple = ABI_INFO[abi]
        src = os.path.join(work, build_dir, "libmarsxlog.so")
        if not os.path.exists(src):
            print("[fail] 缺少产物: %s" % src, file=sys.stderr)
            return 1

        dst = os.path.join(stage, "jni", abi)
        os.makedirs(dst, exist_ok=True)
        so = os.path.join(dst, "libmarsxlog.so")
        shutil.copy(src, so)

        # strip：未 strip 的副本另存（符号化用），再原地精简
        if args.strip:
            sym_dir = os.path.join(out_dir, "symbols", abi)
            os.makedirs(sym_dir, exist_ok=True)
            shutil.copy(src, os.path.join(sym_dir, "libmarsxlog.so"))
            subprocess.check_call([args.strip, "--strip-unneeded", so])

        if args.variant == "full":
            libcxx = os.path.join(sysroot_lib, triple, "libc++_shared.so") if sysroot_lib else ""
            if libcxx and os.path.exists(libcxx):
                shutil.copy(libcxx, os.path.join(dst, "libc++_shared.so"))
            else:
                print("[warn] 未找到 libc++_shared.so: %s" % libcxx, file=sys.stderr)

    # 打包
    os.makedirs(out_dir, exist_ok=True)
    suffix = "" if args.variant == "full" else "-nostl"
    aar_path = os.path.join(out_dir, "mars-xlog-%s%s.aar" % (args.version, suffix))
    if os.path.exists(aar_path):
        os.remove(aar_path)
    with zipfile.ZipFile(aar_path, "w", zipfile.ZIP_DEFLATED) as z:
        for base, _dirs, files in os.walk(stage):
            for name in sorted(files):
                full = os.path.join(base, name)
                rel = os.path.relpath(full, stage).replace("\\", "/")
                z.write(full, rel)
    print("[ok] AAR: %s (%.2f MB)" % (aar_path,
                                     os.path.getsize(aar_path) / 1024 / 1024))

    # 同步 so 到官方 gradle 流程期望的 jniLibs 目录（/mars/libraries/*/ 已在 .gitignore）
    libs = os.path.join(sdk, "libs")
    for abi in abis:
        src_dir = os.path.join(stage, "jni", abi)
        if not os.path.isdir(src_dir):
            continue
        dst_dir = os.path.join(libs, abi)
        os.makedirs(dst_dir, exist_ok=True)
        for name in os.listdir(src_dir):
            shutil.copy(os.path.join(src_dir, name), os.path.join(dst_dir, name))
    print("[ok] so 已同步到 %s" % libs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
