# Android 15 16KB Page Size 适配说明

本 fork 基于 [Tencent/mars](https://github.com/Tencent/mars)，在 `master` 上增加了对 **Android 15 (API 35) 16KB 内存页** 设备的支持，并提供了可直接依赖的 AAR。

## 背景

Android 15 起，部分设备（尤其高端机型）开始使用 16KB 内存页以提升性能。系统要求所有 native 库的 ELF `PT_LOAD` 段按 16KB 对齐，否则 `dlopen` 直接失败，表现为 App 启动崩溃或 `UnsatisfiedLinkError`。

对齐规则（64 位 ABI）：

- 每个 `PT_LOAD` 的 `p_align >= 16384`
- 且 `p_vaddr ≡ p_offset (mod p_align)`

32 位 ABI（`armeabi-v7a`）仍为 4KB 页，无需 16KB 对齐。

## 代码改动

| 文件 | 改动 |
| --- | --- |
| `mars/comm/utils.cmake` | ANDROID 分支新增 `ANDROID_16KB_PAGE_SIZE` 选项（默认 ON），对 `arm64-v8a` / `x86_64` 追加 `-Wl,-z,max-page-size=16384` 到 SHARED/MODULE/EXE 链接标志 |
| `mars/comm/mmap_util.cc` | `OpenMmapFile` 增加映射长度校验，遗留 `.mmap3` 文件被截断时不再越界写导致 SIGBUS，改为降级到堆缓冲 |
| `mars/xlog/src/appender.cc` | 为 `kBufferBlockLength = 150 * 1024` 补注释，说明不可改为 `getpagesize()` 动态值（老 `.mmap3` 文件固定 150KB） |
| `mars/comm/CMakeExtraFlags.txt` | 新增 `ANDROID_ENABLE_WERROR` 选项（默认 ON），用新 NDK 编译旧 boost 时弃用告警不再致命 |
| `mars/CMakeLists.txt` | version script 参数单引号改双引号（Windows `cmd.exe` 不剥离单引号） |
| `mars/libraries/mars_android_sdk/jni/export.exp` | 删除 3 个已失效符号，适配 NDK r27 默认 `--no-undefined-version` |
| `samples/android/xlogSample/.../jni/CMakeLists.txt` | sample 的 `native-lib` 同样加 16KB 链接标志 |

> 注意：xlog 的数据通路本身没有硬编码 4096 页大小。`boost::iostreams::mapped_file` 的 `alignment()` 用 `sysconf(_SC_PAGESIZE)`，已是动态值；xlog 只用 `offset=0` 映射。真正需要改的只有 ELF 对齐。

## 构建

```bash
# 需要 NDK r27+、cmake、ninja
./build_android_aar.sh
```

产物：

- `build/android/mars-xlog-1.2.6.aar` —— 含 `libc++_shared.so`（约 1.4 MB）
- `build/android/mars-xlog-1.2.6-nostl.aar` —— 不含（约 485 KB）

脚本工具：

- `tools/make_xlog_aar.py` —— 手工打 AAR（不依赖 Gradle/AGP），默认 `llvm-strip --strip-unneeded`
- `tools/check_16kb_align.py` —— 纯 Python 解析 ELF program header 校验对齐
- `tools/publish_to_github_packages.py` —— 上传到 GitHub Packages Maven registry

### 关于 so 体积

NDK Release 默认带 `-g`，`.debug_*` 段约 4.7 MB。`llvm-strip --strip-unneeded` 后：

- `arm64-v8a`：7.36 MB → 525 KB
- `armeabi-v7a`：6.3 MB → 432 KB

strip 不影响 `PT_LOAD` 对齐（仍为 `0x4000`），导出符号保留。未 strip 的副本存于 `build/android/symbols/<abi>/`，用于崩溃符号化。

## 验证结果

| ABI | `PT_LOAD` 对齐 | 结论 |
| --- | --- | --- |
| `arm64-v8a` | `0x4000` (16KB) | 通过 |
| `armeabi-v7a` | `0x1000` (4KB) | 通过（32 位本就 4KB 页） |

## 引用方式

包已发布到 GitHub Packages：

```groovy
// settings.gradle 或模块 build.gradle
repositories {
    maven {
        url "https://maven.pkg.github.com/mtzqc/mars"
        credentials {
            username = project.findProperty("gpr.user") ?: System.getenv("GPR_USER")
            password = project.findProperty("gpr.token") ?: System.getenv("GPR_TOKEN")
        }
    }
}
```

```groovy
dependencies {
    // 自带 libc++_shared.so
    implementation 'com.tencent.mars:mars-xlog:1.2.6'

    // 不含 libc++_shared.so，适合 App 已通过其他依赖提供该运行时的场景
    // implementation 'com.tencent.mars:mars-xlog-nostl:1.2.6'
}
```

`gradle.properties`（**不要提交到版本库**）：

```properties
gpr.user=mtzqc
gpr.token=ghp_xxxxxxxx
```

### full 还是 nostl？

- **full（`mars-xlog`）**：AAR 内含 `libc++_shared.so`，开箱即用。若工程中其他库也带了 `libc++_shared.so`，可能出现重复/版本冲突。
- **nostl（`mars-xlog-nostl`）**：不打包 `libc++_shared.so`，体积更小。适用于 App 自身以 `-DANDROID_STL=c++_shared` 编译，或其他依赖已提供该库的情况；否则运行时 `UnsatisfiedLinkError`。

## 应用侧仍需注意

- AGP 8.5.1+ 打包时使用 16KB zipalign，或手动 `zipalign -P 16 -f -v 4 in.apk out.apk`
- 保持 `android.useLegacyPackaging=false`（默认），让 so 以未压缩形式对齐存放
- 若使用 x86_64，需移除 `src/comm` 中 `-Wl,--no-warn-shared-textrel`（gold 专有选项，lld 不识别）
