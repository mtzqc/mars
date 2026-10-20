#!/usr/bin/env bash
# ==============================================================================
# 一键产出 mars-xlog AAR（含 16KB page size 对齐，适配 Android 15）
#
#   用法:
#     ANDROID_NDK_HOME=/path/to/android-ndk ./build_android_aar.sh
#   可选环境变量:
#     OUT_DIR   产物输出目录（默认 build/android）
#     ABIS      需要编译的 ABI，空格分隔（默认 "arm64-v8a armeabi-v7a"）
#     API       最低 API level（默认 21）
#
# 产物:
#     $OUT_DIR/mars-xlog-<version>.aar
#     mars/libraries/mars_xlog_sdk/libs/<abi>/{libmarsxlog.so,libc++_shared.so}
#
# 依赖: cmake(>=3.18)、ninja、NDK、jdk(自带 javac/jar)
# ==============================================================================
set -euo pipefail

MARS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${OUT_DIR:-${MARS_DIR}/build/android}"
ABIS="${ABIS:-arm64-v8a armeabi-v7a}"
API="${API:-21}"
VERSION="$(grep -E '^PROJ_VERSION=' "${MARS_DIR}/mars/libraries/mars_xlog_sdk/gradle.properties" | cut -d= -f2)"

if [ -z "${ANDROID_NDK_HOME:-}" ]; then
    echo "error: 请设置 ANDROID_NDK_HOME 指向 NDK 根目录" >&2
    exit 1
fi
NDK="${ANDROID_NDK_HOME}"
if [ ! -d "${NDK}/build/cmake" ]; then
    echo "error: ${NDK} 不是有效的 NDK 目录" >&2
    exit 1
fi

# ABI -> NDK sysroot 里的 triple
ndk_triple() {
    case "$1" in
        arm64-v8a)   echo "aarch64-linux-android" ;;
        armeabi-v7a) echo "arm-linux-androideabi" ;;
        x86_64)      echo "x86_64-linux-android" ;;
        *) echo "" ;;
    esac
}

WORK="${OUT_DIR}/.work"
mkdir -p "${WORK}" "${OUT_DIR}"

# ---------------------------------------------------------------- 1. 编译 so
for abi in ${ABIS}; do
    echo "==> 编译 ${abi}"
    cmake -S "${MARS_DIR}/mars" -B "${WORK}/${abi}" -G Ninja \
        -DCMAKE_TOOLCHAIN_FILE="${NDK}/build/cmake/android.toolchain.cmake" \
        -DANDROID_ABI="${abi}" \
        -DANDROID_PLATFORM="android-${API}" \
        -DANDROID_STL=c++_shared \
        -DCMAKE_BUILD_TYPE=Release \
        -DANDROID_ENABLE_WERROR=OFF \
        -Wno-dev -Wno-deprecated
    cmake --build "${WORK}/${abi}" --target marsxlog
    cp "${WORK}/${abi}/libmarsxlog.so" "${OUT_DIR}/libmarsxlog-${abi}.so"
done

# ------------------------------------------------------------ 2. 编译 java
echo "==> 编译 java classes"
SDK_JAR="${ANDROID_HOME:-${ANDROID_SDK_ROOT}}/platforms/android-${API}/android.jar"
if [ ! -f "${SDK_JAR}" ]; then
    echo "error: 找不到 android.jar: ${SDK_JAR}（请设置 ANDROID_HOME）" >&2
    exit 1
fi
rm -rf "${WORK}/classes" && mkdir -p "${WORK}/classes"
find "${MARS_DIR}/mars/libraries/mars_xlog_sdk/src/main/java" -name "*.java" > "${WORK}/javafiles.txt"
javac --release 8 -classpath "${SDK_JAR}" -d "${WORK}/classes" @"${WORK}/javafiles.txt"
jar cf "${WORK}/classes.jar" -C "${WORK}/classes" .

# ------------------------------------------------------------ 3. 打包 aar
# 打两种包：full 含 libc++_shared.so；nostl 不含（由宿主 App 自己提供）
STRIP="$(ls "${NDK}"/toolchains/llvm/prebuilt/*/bin/llvm-strip* 2>/dev/null | head -1)"
echo "==> 打包 aar（strip: ${STRIP:-未找到，将保留调试符号）"
for variant in full nostl; do
    python3 "${MARS_DIR}/tools/make_xlog_aar.py" \
        --mars-dir "${MARS_DIR}" --work-dir "${WORK}" \
        --out-dir "${OUT_DIR}" --version "${VERSION}" --abis "${ABIS}" \
        --ndk "${NDK}" --variant "${variant}" --strip "${STRIP}"
done

echo "==> 完成:"
ls -la "${OUT_DIR}"/*.aar
