#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
校验 so 是否满足 Android 15 的 16KB page size 要求（纯 Python 解析 ELF，不依赖 NDK）。

用法:
    python3 tools/check_16kb_align.py <aar 或 .so 或目录> [...]

规则:
    - 64 位 ABI(arm64-v8a/x86_64) 的所有 PT_LOAD 段 p_align 必须 >= 16384
    - 每个 PT_LOAD 必须满足 p_vaddr ≡ p_offset (mod p_align)，否则加载器会拒绝映射
    - 32 位 ABI(armeabi-v7a/x86) 仍是 4KB 页，p_align >= 4096 即可

退出码: 0 全部通过, 1 存在不满足项
"""
import os
import struct
import sys
import tempfile
import zipfile

PT_LOAD = 1
REQUIRED_ALIGN_64 = 16384
REQUIRED_ALIGN_32 = 4096


def parse_elf_loads(data):
    """返回 (is64, [(p_align, p_offset, p_vaddr), ...])，仅 PT_LOAD。"""
    if data[:4] != b"\x7fELF":
        raise ValueError("不是 ELF 文件")
    ei_class = data[4]          # 1=32bit, 2=64bit
    is64 = ei_class == 2
    if data[5] != 1:            # 仅支持小端（Android 全部是小端）
        raise ValueError("仅支持 little-endian ELF")

    if is64:
        e_phoff, = struct.unpack_from("<Q", data, 0x20)
        e_phentsize, e_phnum = struct.unpack_from("<HH", data, 0x36)
    else:
        e_phoff, = struct.unpack_from("<I", data, 0x1C)
        e_phentsize, e_phnum = struct.unpack_from("<HH", data, 0x2A)

    loads = []
    for i in range(e_phnum):
        off = e_phoff + i * e_phentsize
        if is64:
            p_type, _flags, p_offset, p_vaddr, _paddr, _fsz, _msz, p_align = \
                struct.unpack_from("<IIQQQQQQ", data, off)
        else:
            p_type, p_offset, p_vaddr, _paddr, _fsz, _msz, _flags, p_align = \
                struct.unpack_from("<IIIIIIII", data, off)
        if p_type == PT_LOAD:
            loads.append((p_align, p_offset, p_vaddr))
    return is64, loads


def abi_from_path(name):
    for abi in ("arm64-v8a", "armeabi-v7a", "x86_64", "x86"):
        if "/%s/" % abi in name.replace("\\", "/"):
            return abi
    return ""


def check_one(label, data, abi=""):
    is64 = abi in ("arm64-v8a", "x86_64") or (not abi and data[4] == 2)
    need = REQUIRED_ALIGN_64 if is64 else REQUIRED_ALIGN_32
    try:
        is64_elf, loads = parse_elf_loads(data)
    except Exception as exc:                     # noqa: BLE001
        print("  [FAIL] %s: %s" % (label, exc))
        return False
    if not loads:
        print("  [FAIL] %s: 没有 PT_LOAD 段" % label)
        return False

    ok = True
    detail = []
    for p_align, p_offset, p_vaddr in loads:
        aligned = p_align >= need
        congruent = (p_vaddr % p_align) == (p_offset % p_align)
        if not (aligned and congruent):
            ok = False
        detail.append("align=0x%x%s%s" % (
            p_align,
            "" if aligned else "(<%d!)" % need,
            "" if congruent else "(vaddr/offset 不同余!)"))
    print("  [%s] %-34s %s" % ("PASS" if ok else "FAIL", label, " ".join(detail)))
    return ok


def collect(path):
    """产出 (label, bytes, abi)。支持 .aar / .so / 目录。"""
    if os.path.isdir(path):
        for base, _dirs, files in os.walk(path):
            for name in files:
                if name.endswith(".so"):
                    full = os.path.join(base, name)
                    with open(full, "rb") as fp:
                        yield full, fp.read(), abi_from_path(full)
        return
    if path.endswith(".aar") or path.endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                if info.filename.endswith(".so"):
                    yield "%s!%s" % (os.path.basename(path), info.filename), \
                        z.read(info), abi_from_path(info.filename)
        return
    with open(path, "rb") as fp:
        yield os.path.basename(path), fp.read(), abi_from_path(path)


def main():
    targets = sys.argv[1:]
    if not targets:
        print(__doc__)
        return 2

    all_ok = True
    for target in targets:
        if not os.path.exists(target):
            print("[skip] 不存在: %s" % target)
            continue
        print("==> %s" % target)
        for label, data, abi in collect(target):
            if not check_one(label, data, abi):
                all_ok = False

    print("\n结论: %s" % ("全部满足 16KB 页对齐要求 ✅" if all_ok else "存在不满足项 ❌"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
