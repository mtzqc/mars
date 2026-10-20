// Tencent is pleased to support the open source community by making Mars available.
// Copyright (C) 2016 THL A29 Limited, a Tencent company. All rights reserved.

// Licensed under the MIT License (the "License"); you may not use this file except in
// compliance with the License. You may obtain a copy of the License at
// http://opensource.org/licenses/MIT

// Unless required by applicable law or agreed to in writing, software distributed under the License is
// distributed on an "AS IS" basis, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND,
// either express or implied. See the License for the specific language governing permissions and
// limitations under the License.

/*
 * mmap_util.c
 *
 *  Created on: 2016-2-22
 *      Author: yanguoyue
 */

#include "mmap_util.h"

#include <stdio.h>
#include <unistd.h>

#include "boost/filesystem.hpp"

bool IsMmapFileOpenSucc(const boost::iostreams::mapped_file& _mmmap_file) {
    return !_mmmap_file.operator!() && _mmmap_file.is_open();
}

bool OpenMmapFile(const char* _filepath, unsigned int _size, boost::iostreams::mapped_file& _mmmap_file) {
    if (NULL == _filepath || 0 == strnlen(_filepath, 128) || 0 == _size) {
        return false;
    }

    if (IsMmapFileOpenSucc(_mmmap_file)) {
        CloseMmapFile(_mmmap_file);
    }

    if (_mmmap_file.is_open() && _mmmap_file.operator!()) {
        return false;
    }

    boost::iostreams::basic_mapped_file_params<boost::filesystem::path> param;
    param.path = boost::filesystem::path(_filepath);
    param.flags = boost::iostreams::mapped_file_base::readwrite;

    bool file_exist = boost::filesystem::exists(_filepath);
    if (!file_exist) {
        param.new_file_size = _size;
    }

    _mmmap_file.open(param);

    bool is_open = IsMmapFileOpenSucc(_mmmap_file);

    // 文件已存在时 boost 只按实际文件长度建立映射，不会按 _size 重新 truncate。
    // Android 15 的 16KB page size 设备上，mmap 长度不是页大小整数倍时，尾部不足
    // 一页的部分没有真正的磁盘页支撑（文件长度即边界），一旦越界写就是 SIGBUS。
    // 这里校验映射长度，不足时返回失败让调用方降级到堆内存缓冲区，避免写日志时崩溃。
    if (is_open && _mmmap_file.size() < _size) {
        CloseMmapFile(_mmmap_file);
        return false;
    }

#ifndef _WIN32
    if (!file_exist && is_open) {
        // Extending a file with ftruncate, thus creating a big hole, and then filling the hole by mod-ifying a shared
        // mmap() can lead to SIGBUS when no space left the boost library uses ftruncate, so we pre-allocate the file's
        // backing store by writing zero.
        FILE* file = fopen(_filepath, "rb+");
        if (NULL == file) {
            _mmmap_file.close();
            boost::filesystem::remove(_filepath);
            return false;
        }

        char* zero_data = new char[_size];
        memset(zero_data, 0, _size);

        if (_size != fwrite(zero_data, sizeof(char), _size, file)) {
            _mmmap_file.close();
            fclose(file);
            boost::filesystem::remove(_filepath);
            delete[] zero_data;
            return false;
        }
        fclose(file);
        delete[] zero_data;
    }
#endif
    return is_open;
}

void CloseMmapFile(boost::iostreams::mapped_file& _mmmap_file) {
    if (_mmmap_file.is_open()) {
        _mmmap_file.close();
    }
}
