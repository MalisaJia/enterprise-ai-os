//go:build windows

package backup

import "golang.org/x/sys/windows"

// availableDiskBytes 返回 path 所在卷对当前用户可用的字节数。
// Windows 没有 statfs，改用 GetDiskFreeSpaceEx 的 lpFreeBytesAvailable，
// 其语义与 Unix 分支的 Bavail*Bsize 一致（已扣除配额）。
func availableDiskBytes(path string) (int64, error) {
	root, err := windows.UTF16PtrFromString(path)
	if err != nil {
		return 0, err
	}
	var freeAvailable, totalBytes, totalFree uint64
	if err := windows.GetDiskFreeSpaceEx(root, &freeAvailable, &totalBytes, &totalFree); err != nil {
		return 0, err
	}
	return int64(freeAvailable), nil
}
