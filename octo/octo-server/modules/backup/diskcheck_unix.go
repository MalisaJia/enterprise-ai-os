//go:build !windows

package backup

import "syscall"

// availableDiskBytes 返回 path 所在文件系统对非特权用户可用的字节数。
func availableDiskBytes(path string) (int64, error) {
	var stat syscall.Statfs_t
	if err := syscall.Statfs(path, &stat); err != nil {
		return 0, err
	}
	// 可用空间 = 可用块数 * 块大小
	return int64(stat.Bavail) * int64(stat.Bsize), nil
}
