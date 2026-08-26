//go:build windows

package app

import (
	"os"

	"golang.org/x/sys/windows"
)

// filesystemFreeBytes 返回 path 所在卷对当前用户可用的字节数。
// Windows 没有 statfs，改用 GetDiskFreeSpaceEx 取 lpFreeBytesAvailable，
// 语义与 Unix 分支的 Bavail 一致（已扣除配额/保留空间）。
func filesystemFreeBytes(path string) (uint64, error) {
	if path == "" {
		return 0, os.ErrNotExist
	}
	root, err := windows.UTF16PtrFromString(path)
	if err != nil {
		return 0, err
	}
	var freeAvailable, totalBytes, totalFree uint64
	if err := windows.GetDiskFreeSpaceEx(root, &freeAvailable, &totalBytes, &totalFree); err != nil {
		return 0, err
	}
	return freeAvailable, nil
}
