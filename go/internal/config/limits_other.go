//go:build !unix

package config

// DesiredFdLimit 在非 Unix 平台上没有意义，返回 0。
func DesiredFdLimit() uint64 { return 0 }

// RaiseFdLimit 在非 Unix 平台上不做任何事。
func RaiseFdLimit() (before, after uint64, changed bool) { return 0, 0, false }
