//go:build !windows

package app

import (
	"os"
	"syscall"
)

// filesystemFreeBytes 返回 path 所在文件系统对非特权用户可用的字节数。
func filesystemFreeBytes(path string) (uint64, error) {
	if path == "" {
		return 0, os.ErrNotExist
	}
	var stat syscall.Statfs_t
	if err := syscall.Statfs(path, &stat); err != nil {
		return 0, err
	}
	return stat.Bavail * uint64(stat.Bsize), nil
}
