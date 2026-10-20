cmake_minimum_required(VERSION 3.4.1)

include_directories(export_include)

add_library(native-lib SHARED native-lib.cpp)

# Android 15 起部分设备使用 16KB 内存页，APK 内的所有 so 都需要按 16KB 对齐，
# 否则在这些设备上 System.loadLibrary 会失败。
if(ANDROID_ABI STREQUAL "arm64-v8a" OR ANDROID_ABI STREQUAL "x86_64")
    set_target_properties(native-lib PROPERTIES LINK_FLAGS "-Wl,-z,max-page-size=16384")
endif()

find_library(log-lib log)

set(XLOG_PATH ../jniLibs/jni/${ANDROID_ABI}/)

find_library(XLOG_LIB marsxlog PATHS ${XLOG_PATH} NO_DEFAULT_PATH NO_CMAKE_FIND_ROOT_PATH)

target_link_libraries(native-lib ${log-lib} ${XLOG_LIB})
